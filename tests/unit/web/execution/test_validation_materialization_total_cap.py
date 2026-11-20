"""Validation materialization: the total file-held text cap, at its old and new boundaries.

``materialize_validation_yaml`` is called directly. It enforces the cap and then
dumps the resolved configuration; the 2 MiB YAML bound is applied later, when the
runtime phase parses that dump, so these tests measure the cap check alone.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.enums import CreationModality
from elspeth.core.blobs_inline import BLOB_INLINE_AGGREGATE_BYTE_CAP, BLOB_INLINE_PER_REF_BYTE_CAP
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.execution._validation_materialization import materialize_validation_yaml
from elspeth.web.execution._validation_model import (
    AuthoredValidatedState,
    InterpretationValidatedState,
    PhaseFailure,
    PhaseReport,
    PolicyLoweredState,
)

SESSION_ID = UUID("8cf34f4c-27c3-4c51-953a-f679852516a2")
OLD_AGGREGATE_CAP = 1024 * 1024


def inline_blob_total_refusal(total: int, cap: int, bindings: int) -> str:
    """The expected sentence, spelled here so this file collects on the base tree."""
    return (
        f"File-held text totals {total} bytes across {bindings} file bindings; the limit is {cap} bytes. "
        "A file bound in more than one place counts once per place. Remove a binding or shorten the largest files."
    )


class _YamlGenerator:
    def generate_yaml(self, state: CompositionState) -> str:
        del state
        return "blob_ref: inline_content\n"


def _interpretation() -> InterpretationValidatedState:
    state = CompositionState(
        source=SourceSpec(plugin="csv", on_success="node_in", options={}, on_validation_failure="discard"),
        nodes=(),
        edges=(),
        outputs=(OutputSpec(name="primary", plugin="csv", options={}, on_write_failure="discard"),),
        metadata=PipelineMetadata(),
        version=1,
    )
    authored = AuthoredValidatedState(
        policy=PolicyLoweredState(
            authored_state=state,
            state=state,
            profiled_s3_audit_identities=(),
            profiled_textract_audit_identities=(),
            operator_resolved_model_node_ids=frozenset(),
        ),
        all_secret_refs=(),
        env_ref_names=frozenset(),
        semantic_contracts=(),
    )
    return InterpretationValidatedState(authored=authored, materialized_state=state)


def _record(blob_id: UUID, content: bytes) -> BlobRecord:
    return BlobRecord(
        id=blob_id,
        session_id=SESSION_ID,
        filename="notes.txt",
        mime_type="text/plain",
        size_bytes=len(content),
        content_hash=hashlib.sha256(content).hexdigest(),
        storage_path="notes.txt",
        created_at=datetime(2026, 11, 21, tzinfo=UTC),
        created_by="user",
        source_description=None,
        status="ready",
        creation_modality=CreationModality.VERBATIM,
        created_from_message_id=None,
        creating_model_identifier=None,
        creating_model_version=None,
        creating_provider=None,
        creating_composer_skill_hash=None,
        creating_arguments_hash=None,
    )


def _sizes_totalling(total: int) -> list[int]:
    full, remainder = divmod(total, BLOB_INLINE_PER_REF_BYTE_CAP)
    return [BLOB_INLINE_PER_REF_BYTE_CAP] * full + ([remainder] if remainder else [])


def _materialize(bindings: list[tuple[UUID, bytes]]) -> PhaseReport[Any] | PhaseFailure:
    """One transform whose options bind each (blob, content) pair once, in order."""
    records = {blob_id: _record(blob_id, content) for blob_id, content in bindings}
    options = {
        f"file_{index}": {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": records[blob_id].content_hash}
        for index, (blob_id, _content) in enumerate(bindings)
    }
    config = {"transforms": [{"name": "node", "options": options}]}
    contents = dict(bindings)
    return materialize_validation_yaml(
        _interpretation(),
        yaml_generator=_YamlGenerator(),
        data_dir=Path("data"),
        session_id="test-session",
        blob_get_metadata=records.get,
        blob_get_content=lambda blob_id: (records[blob_id], contents[blob_id]),
        load_yaml=lambda _pipeline_yaml: config,
    )


def _distinct(sizes: list[int]) -> list[tuple[UUID, bytes]]:
    return [(uuid4(), bytes([65 + index % 26]) * size) for index, size in enumerate(sizes)]


@pytest.mark.parametrize(
    "total",
    [
        pytest.param(OLD_AGGREGATE_CAP + 1, id="old-cap-plus-one"),
        pytest.param(BLOB_INLINE_AGGREGATE_BYTE_CAP, id="exactly-cap"),
    ],
)
def test_totals_up_to_the_cap_validate(total: int) -> None:
    result = _materialize(_distinct(_sizes_totalling(total)))

    assert isinstance(result, PhaseReport), result
    assert [(check.name, check.passed, check.detail) for check in result.checks] == [
        ("blob_inline_refs", True, "All inline-content blob references and bytes are valid")
    ]


def test_one_byte_over_the_cap_is_refused_with_total_cap_and_count() -> None:
    sizes = [*_sizes_totalling(BLOB_INLINE_AGGREGATE_BYTE_CAP), 1]

    result = _materialize(_distinct(sizes))

    assert isinstance(result, PhaseFailure)
    assert result.failed_check.name == "blob_inline_refs"
    assert result.failed_check.detail == "(aggregate): oversized"
    assert [(error.error_code, error.component_id, error.component_type) for error in result.errors] == [
        ("oversized_inline_blob_content", None, None)
    ]
    (error,) = result.errors
    assert error.message == inline_blob_total_refusal(BLOB_INLINE_AGGREGATE_BYTE_CAP + 1, BLOB_INLINE_AGGREGATE_BYTE_CAP, len(sizes))
    assert error.suggestion == (
        "Remove a file binding or shorten the largest files until the total is at or under the limit; "
        "each file counts once for every place it is bound."
    )


def test_the_total_is_refused_from_metadata_before_any_byte_is_read() -> None:
    """The declared sizes decide: no file content is read for a pipeline over the total."""
    sizes = [*_sizes_totalling(BLOB_INLINE_AGGREGATE_BYTE_CAP), 1]
    bindings = _distinct(sizes)
    records = {blob_id: _record(blob_id, content) for blob_id, content in bindings}
    options = {
        f"file_{index}": {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": records[blob_id].content_hash}
        for index, (blob_id, _content) in enumerate(bindings)
    }
    reads: list[UUID] = []

    def read_content(blob_id: UUID) -> tuple[BlobRecord, bytes]:
        reads.append(blob_id)
        return records[blob_id], dict(bindings)[blob_id]

    result = materialize_validation_yaml(
        _interpretation(),
        yaml_generator=_YamlGenerator(),
        data_dir=Path("data"),
        session_id="test-session",
        blob_get_metadata=records.get,
        blob_get_content=read_content,
        load_yaml=lambda _pipeline_yaml: {"transforms": [{"name": "node", "options": options}]},
    )

    assert isinstance(result, PhaseFailure)
    assert [error.error_code for error in result.errors] == ["oversized_inline_blob_content"]
    assert reads == []


@pytest.mark.parametrize(
    ("bindings", "admitted"),
    [
        pytest.param(BLOB_INLINE_AGGREGATE_BYTE_CAP // BLOB_INLINE_PER_REF_BYTE_CAP, True, id="cap-exactly"),
        pytest.param(BLOB_INLINE_AGGREGATE_BYTE_CAP // BLOB_INLINE_PER_REF_BYTE_CAP + 1, False, id="one-more-binding"),
    ],
)
def test_one_file_bound_in_many_places_counts_once_per_binding(bindings: int, admitted: bool) -> None:
    blob = (uuid4(), b"r" * BLOB_INLINE_PER_REF_BYTE_CAP)

    result = _materialize([blob] * bindings)

    if admitted:
        assert isinstance(result, PhaseReport), result
        return
    assert isinstance(result, PhaseFailure)
    assert [error.message for error in result.errors] == [
        inline_blob_total_refusal(bindings * BLOB_INLINE_PER_REF_BYTE_CAP, BLOB_INLINE_AGGREGATE_BYTE_CAP, bindings)
    ]


def test_per_file_cap_is_unchanged() -> None:
    admitted = _materialize(_distinct([BLOB_INLINE_PER_REF_BYTE_CAP]))
    refused = _materialize(_distinct([BLOB_INLINE_PER_REF_BYTE_CAP + 1]))

    assert isinstance(admitted, PhaseReport), admitted
    assert isinstance(refused, PhaseFailure)
    assert [(error.error_code, error.message) for error in refused.errors] == [
        (
            "oversized_inline_blob_content",
            "Inline content blob reference at node:node.options.file_0 is oversized: "
            f"{BLOB_INLINE_PER_REF_BYTE_CAP + 1} bytes exceeds per-ref cap {BLOB_INLINE_PER_REF_BYTE_CAP}",
        )
    ]
