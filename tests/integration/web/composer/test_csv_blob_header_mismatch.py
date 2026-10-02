"""Integration coverage for CSV source proof diagnostics on blob-backed previews."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.web.blobs.service import content_hash
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer.source_inspection import SourceInspectionFacts, inspect_csv_source_content
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools import execute_tool
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.execution.schemas import ValidationCheck, ValidationReadiness, ValidationResult
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import blobs_table, sessions_table
from elspeth.web.sessions.schema import initialize_session_schema
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.session_fences import fenced_operation_context

_HEADER_MISMATCH_CODE = "csv_source_blob_header_mismatch"
_HEADER_RESOLUTION_ERROR_CODE = "csv_source_field_resolution_error"


def _catalog() -> PolicyCatalogView:
    manager = PluginManager()
    manager.register_builtin_plugins()
    full_catalog = CatalogServiceImpl(manager)
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(full_catalog)
    return PolicyCatalogView.for_trained_operator(full_catalog, snapshot)


def _empty_state() -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _session_engine() -> tuple[Engine, str]:
    engine = create_session_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="test-user")
    session_id = str(uuid4())
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            sessions_table.insert().values(
                id=session_id,
                user_id="test-user",
                auth_provider_type="local",
                title="CSV proof diagnostic test",
                created_at=now,
                updated_at=now,
            )
        )
    return engine, session_id


def _insert_blob(
    engine: Engine,
    session_id: str,
    tmp_path: Path,
    *,
    filename: str,
    mime_type: str,
    content: bytes,
) -> str:
    blob_id = str(uuid4())
    storage_dir = tmp_path / "blobs" / session_id
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_path = storage_dir / f"{blob_id}_{filename}"
    storage_path.write_bytes(content)
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            blobs_table.insert().values(
                id=blob_id,
                session_id=session_id,
                filename=filename,
                mime_type=mime_type,
                size_bytes=len(content),
                content_hash=content_hash(content),
                storage_path=str(storage_path),
                created_at=now,
                created_by="user",
                source_description=None,
                status="ready",
            )
        )
    return blob_id


def _state_with_blob_source(
    engine: Engine,
    session_id: str,
    blob_id: str,
    *,
    data_dir: Path,
    plugin: str,
    options: dict[str, Any],
) -> CompositionState:
    catalog = _catalog()
    with fenced_operation_context(engine, session_id) as context:
        result = execute_tool(
            "set_source_from_blob",
            {
                "blob_id": blob_id,
                "plugin": plugin,
                "on_success": "out",
                "on_validation_failure": "discard",
                "options": options,
            },
            _empty_state(),
            catalog,
            plugin_snapshot=catalog.snapshot,
            data_dir=str(data_dir),
            session_engine=engine,
            session_id=session_id,
            session_operation_context=context,
            session_operation_authority=SQLiteLocalSessionOperationAuthority(engine),
        )
    assert result.success is True, result.data

    result = execute_tool(
        "set_output",
        {
            "sink_name": "out",
            "plugin": "json",
            "options": {
                "path": "outputs/out.json",
                "schema": {"mode": "observed"},
                "mode": "write",
                "collision_policy": "auto_increment",
            },
            "on_write_failure": "discard",
        },
        result.updated_state,
        catalog,
        plugin_snapshot=catalog.snapshot,
    )
    assert result.success is True, result.data
    return result.updated_state


def _passing_runtime_preflight(_state: CompositionState) -> ValidationResult:
    """Hold Stage 2 constant so Stage 3 is the only stage moving the verdict.

    ``preview_pipeline`` fails closed when no preflight is wired, which would
    make every case in this file red for a reason that has nothing to do with
    the proof diagnostics it exists to pin.
    """
    return ValidationResult(
        is_valid=True,
        checks=[ValidationCheck(name="settings_load", passed=True, detail="Settings loaded.", affected_nodes=(), outcome_code=None)],
        errors=[],
        readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
    )


def _preview_data(engine: Engine, session_id: str, state: CompositionState, *, data_dir: Path) -> dict[str, Any]:
    catalog = _catalog()
    with fenced_operation_context(engine, session_id) as operation:
        result = execute_tool(
            "preview_pipeline",
            {},
            state,
            catalog,
            plugin_snapshot=catalog.snapshot,
            session_engine=engine,
            session_id=session_id,
            session_operation_context=operation,
            session_operation_authority=SQLiteLocalSessionOperationAuthority(engine),
            data_dir=str(data_dir),
            runtime_preflight=_passing_runtime_preflight,
        )
    assert result.success is True, result.data
    return result.data


@pytest.mark.parametrize("schema_mode", ["fixed", "flexible"])
def test_csv_blob_without_header_and_no_declared_overlap_blocks(schema_mode: str, tmp_path: Path) -> None:
    engine, session_id = _session_engine()
    blob_id = _insert_blob(
        engine,
        session_id,
        tmp_path,
        filename="web_pages.txt",
        mime_type="text/plain",
        content=b"https://www.finance.gov.au\nhttps://www.ato.gov.au\n",
    )
    state = _state_with_blob_source(
        engine,
        session_id,
        blob_id,
        data_dir=tmp_path,
        plugin="csv",
        options={"schema": {"mode": schema_mode, "fields": ["url: str"]}},
    )

    data = _preview_data(engine, session_id, state, data_dir=tmp_path)

    diagnostics = data["proof_diagnostics"]
    matching = [item for item in diagnostics if item["code"] == _HEADER_MISMATCH_CODE]
    assert matching
    assert matching[0]["severity"] == "blocking"
    diagnostic_blob = repr(matching[0])
    assert "https://www.finance.gov.au" not in diagnostic_blob
    assert "https://www.ato.gov.au" not in diagnostic_blob
    assert matching[0]["evidence_locator"]["observed_header_count"] == 1
    assert matching[0]["evidence_locator"]["observed_headers_redacted"] is True
    assert "observed_headers" not in matching[0]["evidence_locator"]
    assert data["preview_is_valid"] is False


def test_csv_blob_with_matching_header_does_not_block(tmp_path: Path) -> None:
    engine, session_id = _session_engine()
    blob_id = _insert_blob(
        engine,
        session_id,
        tmp_path,
        filename="web_pages.csv",
        mime_type="text/csv",
        content=b"url\nhttps://www.finance.gov.au\n",
    )
    state = _state_with_blob_source(
        engine,
        session_id,
        blob_id,
        data_dir=tmp_path,
        plugin="csv",
        options={"schema": {"mode": "fixed", "fields": ["url: str"]}},
    )

    data = _preview_data(engine, session_id, state, data_dir=tmp_path)

    codes = [item["code"] for item in data["proof_diagnostics"]]
    assert _HEADER_MISMATCH_CODE not in codes
    assert data["preview_is_valid"] is True


def test_csv_blob_with_normalized_header_does_not_block(tmp_path: Path) -> None:
    engine, session_id = _session_engine()
    blob_id = _insert_blob(
        engine,
        session_id,
        tmp_path,
        filename="customers.csv",
        mime_type="text/csv",
        content=b"Customer ID\n123\n",
    )
    state = _state_with_blob_source(
        engine,
        session_id,
        blob_id,
        data_dir=tmp_path,
        plugin="csv",
        options={"schema": {"mode": "fixed", "fields": ["customer_id: str"]}},
    )

    data = _preview_data(engine, session_id, state, data_dir=tmp_path)

    codes = [item["code"] for item in data["proof_diagnostics"]]
    assert _HEADER_MISMATCH_CODE not in codes
    assert data["preview_is_valid"] is True


def test_csv_blob_with_field_mapping_header_does_not_block(tmp_path: Path) -> None:
    engine, session_id = _session_engine()
    blob_id = _insert_blob(
        engine,
        session_id,
        tmp_path,
        filename="customers.csv",
        mime_type="text/csv",
        content=b"External ID\n123\n",
    )
    state = _state_with_blob_source(
        engine,
        session_id,
        blob_id,
        data_dir=tmp_path,
        plugin="csv",
        options={
            "field_mapping": {"external_id": "customer_id"},
            "schema": {"mode": "fixed", "fields": ["customer_id: str"]},
        },
    )

    data = _preview_data(engine, session_id, state, data_dir=tmp_path)

    codes = [item["code"] for item in data["proof_diagnostics"]]
    assert _HEADER_MISMATCH_CODE not in codes
    assert data["preview_is_valid"] is True


def test_csv_blob_with_normalization_collision_returns_blocking_diagnostic(tmp_path: Path) -> None:
    engine, session_id = _session_engine()
    blob_id = _insert_blob(
        engine,
        session_id,
        tmp_path,
        filename="customers.csv",
        mime_type="text/csv",
        content=b"Customer ID,customer_id\n123,456\n",
    )
    state = _state_with_blob_source(
        engine,
        session_id,
        blob_id,
        data_dir=tmp_path,
        plugin="csv",
        options={"schema": {"mode": "fixed", "fields": ["customer_id: str"]}},
    )

    data = _preview_data(engine, session_id, state, data_dir=tmp_path)

    matching = [item for item in data["proof_diagnostics"] if item["code"] == _HEADER_RESOLUTION_ERROR_CODE]
    assert matching
    assert matching[0]["severity"] == "blocking"
    assert data["preview_is_valid"] is False
    # The raw resolver exception text (which quotes the colliding header values)
    # must NOT be echoed — header-resolution failure means a headerless/malformed
    # CSV can make a data row look like headers, so observed values are withheld.
    diagnostic_blob = repr(matching[0])
    assert "Customer ID" not in diagnostic_blob
    assert matching[0]["evidence_locator"]["observed_headers_redacted"] is True
    assert "observed_headers" not in matching[0]["evidence_locator"]


@pytest.mark.parametrize("schema_mode", ["observed", "fixed", "flexible"])
@pytest.mark.parametrize("header", [b"Case Study,case_study", b",id", b"!!!,id"])
def test_csv_unresolvable_headers_block_every_schema_mode(schema_mode: str, header: bytes, tmp_path: Path) -> None:
    engine, session_id = _session_engine()
    try:
        blob_id = _insert_blob(
            engine,
            session_id,
            tmp_path,
            filename="invalid.csv",
            mime_type="text/csv",
            content=header + b"\nROW_VALUE_SENTINEL_ALPHA_82E7,ROW_VALUE_SENTINEL_BETA_82E7\n",
        )
        schema: dict[str, Any] = {"mode": schema_mode}
        if schema_mode != "observed":
            schema["fields"] = ["id: str"]
        state = _state_with_blob_source(engine, session_id, blob_id, data_dir=tmp_path, plugin="csv", options={"schema": schema})

        data = _preview_data(engine, session_id, state, data_dir=tmp_path)

        diagnostics = data["proof_diagnostics"]
        matching = [item for item in diagnostics if item["code"] == _HEADER_RESOLUTION_ERROR_CODE]
        assert len(matching) == 1
        assert matching[0]["severity"] == "blocking"
        assert matching[0]["evidence_locator"]["observed_header_count"] == 2
        assert matching[0]["evidence_locator"]["observed_headers_redacted"] is True
        assert "observed_headers" not in matching[0]["evidence_locator"]
        assert data["preview_is_valid"] is False
        assert _HEADER_MISMATCH_CODE not in [item["code"] for item in diagnostics]
        assert "csv_fixed_schema_omits_observed_columns" not in [item["code"] for item in diagnostics]
        assert "ROW_VALUE_SENTINEL_ALPHA_82E7" not in repr(diagnostics)
        assert "ROW_VALUE_SENTINEL_BETA_82E7" not in repr(diagnostics)
    finally:
        engine.dispose()


def test_csv_normalization_warning_redacts_regressed_details(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The final preview boundary withholds even regressed detector text."""
    import elspeth.web.composer.tools.generation as generation_module

    raw_warning_sentinel = "RAW_NORMALIZATION_WARNING_82E7"

    def inspect_with_regressed_warning(**kwargs: Any) -> SourceInspectionFacts:
        facts = inspect_csv_source_content(**kwargs)
        assert any(warning.startswith("csv_field_normalization_failed:") for warning in facts.warnings)
        return replace(facts, warnings=(f"csv_field_normalization_failed: {raw_warning_sentinel}",))

    monkeypatch.setattr(generation_module, "inspect_csv_source_content", inspect_with_regressed_warning)
    engine, session_id = _session_engine()
    try:
        blob_id = _insert_blob(
            engine,
            session_id,
            tmp_path,
            filename="invalid.csv",
            mime_type="text/csv",
            content=b"RAW HEADER SENTINEL,raw_header_sentinel\n123,456\n",
        )
        state = _state_with_blob_source(
            engine, session_id, blob_id, data_dir=tmp_path, plugin="csv", options={"schema": {"mode": "observed"}}
        )

        data = _preview_data(engine, session_id, state, data_dir=tmp_path)

        diagnostics = data["proof_diagnostics"]
        matching = [item for item in diagnostics if item["code"] == _HEADER_RESOLUTION_ERROR_CODE]
        assert len(matching) == 1
        assert matching[0]["severity"] == "blocking"
        assert matching[0]["evidence_locator"]["observed_headers_redacted"] is True
        assert raw_warning_sentinel not in repr(diagnostics)
        assert "RAW HEADER SENTINEL" not in repr(diagnostics)
        assert "raw_header_sentinel" not in repr(diagnostics)
        assert data["preview_is_valid"] is False
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("content", "warning_code"),
    [
        (b"Field" + b"x" * 9000 + b"\nvalue\n", "csv_header_sample_truncated:"),
        (b"\xff,id\nvalue,other\n", "csv_header_decode_failed:"),
    ],
    ids=("truncated_header", "undecodable_header"),
)
def test_csv_uncertified_header_sample_is_not_a_normalization_failure(content: bytes, warning_code: str, tmp_path: Path) -> None:
    """Hold preflight green to prove the proof stage preserves its abstention."""
    engine, session_id = _session_engine()
    try:
        blob_id = _insert_blob(engine, session_id, tmp_path, filename="sample.csv", mime_type="text/csv", content=content)
        state = _state_with_blob_source(
            engine, session_id, blob_id, data_dir=tmp_path, plugin="csv", options={"schema": {"mode": "observed"}}
        )

        data = _preview_data(engine, session_id, state, data_dir=tmp_path)

        diagnostics = data["proof_diagnostics"]
        advisory = [
            item for item in diagnostics if item["code"] == "source_inspection_warning" and item["message"].startswith(warning_code)
        ]
        assert len(advisory) == 1
        assert advisory[0]["severity"] == "info"
        assert _HEADER_RESOLUTION_ERROR_CODE not in [item["code"] for item in diagnostics]
        assert data["preview_is_valid"] is True
    finally:
        engine.dispose()


def test_text_source_with_csv_named_blob_does_not_require_csv_header_normalization(tmp_path: Path) -> None:
    engine, session_id = _session_engine()
    try:
        blob_id = _insert_blob(
            engine, session_id, tmp_path, filename="customers.csv", mime_type="text/csv", content=b"Customer ID,customer_id\n123,456\n"
        )
        state = _state_with_blob_source(
            engine, session_id, blob_id, data_dir=tmp_path, plugin="text", options={"column": "text", "schema": {"mode": "observed"}}
        )

        data = _preview_data(engine, session_id, state, data_dir=tmp_path)

        diagnostics = data["proof_diagnostics"]
        assert _HEADER_RESOLUTION_ERROR_CODE not in [item["code"] for item in diagnostics]
        advisory = [
            item
            for item in diagnostics
            if item["code"] == "source_inspection_warning" and item["message"].startswith("csv_field_normalization_failed:")
        ]
        assert len(advisory) == 1
        assert advisory[0]["severity"] == "info"
        assert data["preview_is_valid"] is True
    finally:
        engine.dispose()


def test_csv_blob_with_invalid_field_mapping_returns_blocking_diagnostic(tmp_path: Path) -> None:
    engine, session_id = _session_engine()
    blob_id = _insert_blob(
        engine,
        session_id,
        tmp_path,
        filename="customers.csv",
        mime_type="text/csv",
        content=b"External ID\n123\n",
    )
    state = _state_with_blob_source(
        engine,
        session_id,
        blob_id,
        data_dir=tmp_path,
        plugin="csv",
        options={
            "field_mapping": {"missing_header": "customer_id"},
            "schema": {"mode": "fixed", "fields": ["customer_id: str"]},
        },
    )

    data = _preview_data(engine, session_id, state, data_dir=tmp_path)

    matching = [item for item in data["proof_diagnostics"] if item["code"] == _HEADER_RESOLUTION_ERROR_CODE]
    assert matching
    assert matching[0]["severity"] == "blocking"
    assert data["preview_is_valid"] is False
    # Raw resolver text (which quotes the unmatched field_mapping keys / headers)
    # is withheld; observed values are redacted to a count.
    diagnostic_blob = repr(matching[0])
    assert "missing_header" not in diagnostic_blob
    assert "External ID" not in diagnostic_blob
    assert matching[0]["evidence_locator"]["observed_headers_redacted"] is True


def test_csv_blob_headerless_columns_mode_does_not_block(tmp_path: Path) -> None:
    engine, session_id = _session_engine()
    blob_id = _insert_blob(
        engine,
        session_id,
        tmp_path,
        filename="web_pages.txt",
        mime_type="text/plain",
        content=b"https://www.finance.gov.au\nhttps://www.ato.gov.au\n",
    )
    state = _state_with_blob_source(
        engine,
        session_id,
        blob_id,
        data_dir=tmp_path,
        plugin="csv",
        options={
            "columns": ["url"],
            "schema": {"mode": "fixed", "fields": ["url: str"]},
        },
    )

    data = _preview_data(engine, session_id, state, data_dir=tmp_path)

    codes = [item["code"] for item in data["proof_diagnostics"]]
    assert _HEADER_MISMATCH_CODE not in codes
    assert data["preview_is_valid"] is True


def test_csv_fixed_schema_omits_columns_redacts_observed_values(tmp_path: Path) -> None:
    """The fixed-schema-omits diagnostic must not echo observed column values.

    A column header can itself be a data value (an email/token in a headerless
    or mislabelled CSV). The ``csv_fixed_schema_omits_observed_columns``
    diagnostic fires when observed columns are not declared in a fixed schema;
    it must report counts, not the raw observed/missing column values — mirroring
    the deliberate redaction of the sibling header-mismatch diagnostic.
    """
    engine, session_id = _session_engine()
    blob_id = _insert_blob(
        engine,
        session_id,
        tmp_path,
        filename="leaky.csv",
        mime_type="text/csv",
        # Second header is a PII-shaped value masquerading as a column name.
        content=b"token,leaked@example.com\nA,B\n",
    )
    state = _state_with_blob_source(
        engine,
        session_id,
        blob_id,
        data_dir=tmp_path,
        plugin="csv",
        options={"schema": {"mode": "fixed", "fields": ["token: str"]}},
    )

    data = _preview_data(engine, session_id, state, data_dir=tmp_path)

    matching = [item for item in data["proof_diagnostics"] if item["code"] == "csv_fixed_schema_omits_observed_columns"]
    assert matching, [d["code"] for d in data["proof_diagnostics"]]
    assert matching[0]["severity"] == "blocking"
    diagnostic_blob = repr(matching[0])
    assert "leaked@example.com" not in diagnostic_blob
    ev = matching[0]["evidence_locator"]
    assert ev["observed_columns_redacted"] is True
    assert "observed_columns" not in ev
    assert ev["missing_column_count"] >= 1
    assert "missing_columns" not in ev
    assert data["preview_is_valid"] is False


def test_jsonl_blob_does_not_fire_csv_header_mismatch(tmp_path: Path) -> None:
    engine, session_id = _session_engine()
    blob_id = _insert_blob(
        engine,
        session_id,
        tmp_path,
        filename="web_pages.jsonl",
        mime_type="application/x-jsonlines",
        content=b'{"url": "https://www.finance.gov.au"}\n',
    )
    state = _state_with_blob_source(
        engine,
        session_id,
        blob_id,
        data_dir=tmp_path,
        plugin="json",
        options={"schema": {"mode": "fixed", "fields": ["url: str"]}},
    )

    data = _preview_data(engine, session_id, state, data_dir=tmp_path)

    codes = [item["code"] for item in data["proof_diagnostics"]]
    assert _HEADER_MISMATCH_CODE not in codes
    assert data["preview_is_valid"] is True
