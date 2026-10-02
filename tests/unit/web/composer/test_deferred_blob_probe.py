"""Authoring probes distinguish unresolved blob content from invalid config."""

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from elspeth.web.composer import yaml_generator
from elspeth.web.execution.validation import validate_pipeline_for_trained_operator
from tests.unit.web.execution.test_validate_blob_inline import BLOB_ID, _ready_blob_record, _state_with_reference_join


@pytest.mark.parametrize("required", [False, True])
def test_blob_contract_probe_reports_deferral_without_promoting_guarantees(tmp_path: Path, required: bool) -> None:
    state = _state_with_reference_join(
        tmp_path,
        session_id=uuid4(),
        reference_format="csv",
        content={"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": "a" * 64},
    )
    # Force the producer-contract path while keeping the authored guarantee
    # deliberately unproven until the table is read under its authorization.
    state = replace(
        state,
        nodes=(
            replace(
                state.nodes[0], options={**state.nodes[0].options, "schema": {"mode": "observed", "guaranteed_fields": ["description"]}}
            ),
        ),
        outputs=(
            replace(
                state.outputs[0],
                options={
                    **state.outputs[0].options,
                    "schema": {"mode": "observed", "required_fields": ["description"]}
                    if required
                    else {"mode": "fixed", "fields": ["description: str"]},
                },
            ),
        ),
    )

    summary = state.validate()

    deferred = [warning for warning in summary.warnings if warning.error_code == "contract_probe_deferred"]
    assert len(deferred) == 1
    assert deferred[0].severity == "medium"
    assert "authorized blob materialization" in deferred[0].message
    assert not any("pipeline rejected" in warning.message for warning in summary.warnings)
    if required:
        # An author claiming the field is guaranteed must not turn unknown
        # bytes into a successful contract proof.
        assert not summary.is_valid
        assert any("description" in error.message for error in summary.errors)


def test_malformed_blob_marker_remains_a_config_probe_failure(tmp_path: Path) -> None:
    state = _state_with_reference_join(
        tmp_path,
        session_id=uuid4(),
        reference_format="csv",
        content={"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": "not-a-hash"},
    )
    state = replace(
        state,
        outputs=(
            replace(
                state.outputs[0], options={**state.outputs[0].options, "schema": {"mode": "observed", "required_fields": ["description"]}}
            ),
        ),
    )

    summary = state.validate()

    assert not summary.is_valid
    assert not any(warning.error_code == "contract_probe_deferred" for warning in summary.warnings)
    assert any(warning.error_code == "contract_probe_failed" and warning.severity == "high" for warning in summary.warnings)
    assert not any("pipeline rejected" in warning.message for warning in summary.warnings)


@pytest.mark.parametrize(
    ("table", "expected_valid"),
    [(b"sku,description\nhats,A fine hat\n", True), (b"sku,description\n", False), (b"sku,description\nhats\n", False)],
)
def test_deferred_reference_table_requires_real_materialized_validation(tmp_path: Path, table: bytes, expected_valid: bool) -> None:
    session_id = uuid4()
    digest = hashlib.sha256(table).hexdigest()
    state = _state_with_reference_join(
        tmp_path,
        session_id=session_id,
        reference_format="csv",
        content={"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": digest},
    )
    record = replace(_ready_blob_record(session_id=session_id, size_bytes=len(table)), content_hash=digest, mime_type="text/csv")

    result = validate_pipeline_for_trained_operator(
        state,
        SimpleNamespace(data_dir=tmp_path),
        yaml_generator,
        blob_get_metadata=lambda _blob_id: record,
        blob_get_content=lambda _blob_id: (record, table),
        session_id=str(session_id),
    )

    assert result.is_valid is expected_valid, result.errors
    if not expected_valid:
        assert result.errors


def test_deferred_reference_table_without_blob_authority_cannot_be_execution_ready(tmp_path: Path) -> None:
    session_id = uuid4()
    state = _state_with_reference_join(
        tmp_path,
        session_id=session_id,
        reference_format="csv",
        content={"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": "a" * 64},
    )

    result = validate_pipeline_for_trained_operator(state, SimpleNamespace(data_dir=tmp_path), yaml_generator, session_id=str(session_id))

    assert not result.is_valid
    assert not result.readiness.execution_ready
    assert any(check.name == "blob_inline_refs" and not check.passed for check in result.checks)


def test_unused_inline_default_does_not_discard_a_computed_contract(tmp_path: Path) -> None:
    state = _state_with_reference_join(tmp_path, session_id=uuid4(), reference_format="csv", content="sku,description\nhats,A fine hat\n")
    state = replace(
        state,
        nodes=(
            replace(
                state.nodes[0],
                options={
                    **state.nodes[0].options,
                    "on_miss": "fail",
                    "default_values": {"description": {"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": "a" * 64}},
                },
            ),
        ),
        outputs=(
            replace(
                state.outputs[0], options={**state.outputs[0].options, "schema": {"mode": "observed", "required_fields": ["description"]}}
            ),
        ),
    )

    summary = state.validate()

    assert summary.is_valid, summary.errors
    assert not any(warning.error_code == "contract_probe_deferred" for warning in summary.warnings)


@pytest.mark.parametrize("invalid_field", ["on_miss", "unknown_option"])
def test_inline_marker_does_not_reclassify_an_unrelated_invalid_option(tmp_path: Path, invalid_field: str) -> None:
    state = _state_with_reference_join(
        tmp_path,
        session_id=uuid4(),
        reference_format="csv",
        content={"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": "a" * 64},
    )
    state = replace(
        state,
        nodes=(
            replace(
                state.nodes[0],
                options={
                    **state.nodes[0].options,
                    invalid_field: "not-a-policy"
                    if invalid_field == "on_miss"
                    else {"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": "a" * 64},
                },
            ),
        ),
        outputs=(
            replace(
                state.outputs[0], options={**state.outputs[0].options, "schema": {"mode": "observed", "required_fields": ["description"]}}
            ),
        ),
    )

    summary = state.validate()

    assert not summary.is_valid
    assert any(warning.error_code == "contract_probe_failed" for warning in summary.warnings)
    assert not any(warning.error_code == "contract_probe_deferred" for warning in summary.warnings)
