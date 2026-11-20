"""The total cap on file-held text one pipeline may resolve (0.8.16: 1 MiB -> 4 MiB).

Every size here is derived from the two shared constants, so the boundaries
move with them; the one literal pin is the value itself. The total is counted
once per REFERENCE: a file bound in two places is resolved, and recorded in the
run's audit record, twice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.blobs_inline import BlobContentResolutionError, BlobInlineRef
from elspeth.contracts.enums import CreationModality
from elspeth.core.blobs_inline import (
    BLOB_INLINE_AGGREGATE_BYTE_CAP,
    BLOB_INLINE_PER_REF_BYTE_CAP,
    _enforce_blob_content_ref_metadata,
    _validate_blob_content_refs_sync,
)

SESSION_ID = UUID("8cf34f4c-27c3-4c51-953a-f679852516a2")
OLD_AGGREGATE_CAP = 1024 * 1024
# Spelled here, not imported, so this file collects on the base tree.
AGGREGATE_PATH = "(aggregate)"


def _total_refusal(total: int, cap: int, bindings: int) -> str:
    return (
        f"File-held text totals {total} bytes across {bindings} file bindings; the limit is {cap} bytes. "
        "A file bound in more than one place counts once per place. Remove a binding or shorten the largest files."
    )


def _record(blob_id: UUID, size_bytes: int, content_hash: str) -> BlobRecord:
    return BlobRecord(
        id=blob_id,
        session_id=SESSION_ID,
        filename="prompt.txt",
        mime_type="text/plain",
        size_bytes=size_bytes,
        content_hash=content_hash,
        storage_path="prompt.txt",
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
    """Whole per-file-cap files plus one remainder file: every file is under the per-file cap."""
    full, remainder = divmod(total, BLOB_INLINE_PER_REF_BYTE_CAP)
    return [BLOB_INLINE_PER_REF_BYTE_CAP] * full + ([remainder] if remainder else [])


def _distinct_files(sizes: list[int]) -> tuple[list[BlobInlineRef], dict[UUID, BlobRecord], dict[str, object]]:
    refs: list[BlobInlineRef] = []
    records: dict[UUID, BlobRecord] = {}
    options: dict[str, object] = {}
    for index, size in enumerate(sizes):
        blob_id = uuid4()
        digest = f"{index:064x}"
        field = f"file_{index}"
        refs.append(BlobInlineRef(blob_id=blob_id, sha256=digest, field_path=f"source.options.{field}", encoding="utf-8"))
        records[blob_id] = _record(blob_id, size, digest)
        options[field] = {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": digest}
    return refs, records, {"source": {"options": options}}


def test_cap_value_is_four_mib_and_per_file_cap_is_unchanged() -> None:
    assert BLOB_INLINE_AGGREGATE_BYTE_CAP == 4 * 1024 * 1024
    assert BLOB_INLINE_PER_REF_BYTE_CAP == 256 * 1024
    # The new boundary is reachable with files that each meet the per-file cap.
    assert BLOB_INLINE_AGGREGATE_BYTE_CAP % BLOB_INLINE_PER_REF_BYTE_CAP == 0


@pytest.mark.parametrize(
    "total",
    [
        pytest.param(OLD_AGGREGATE_CAP + 1, id="old-cap-plus-one"),
        pytest.param(3 * 1024 * 1024, id="three-mib"),
        pytest.param(BLOB_INLINE_AGGREGATE_BYTE_CAP, id="exactly-cap"),
    ],
)
def test_totals_up_to_the_cap_are_admitted(total: int) -> None:
    refs, records, config = _distinct_files(_sizes_totalling(total))

    _enforce_blob_content_ref_metadata(
        refs, records, per_ref_byte_cap=BLOB_INLINE_PER_REF_BYTE_CAP, aggregate_byte_cap=BLOB_INLINE_AGGREGATE_BYTE_CAP
    )
    violations = _validate_blob_content_refs_sync(
        records.get, config, per_ref_byte_cap=BLOB_INLINE_PER_REF_BYTE_CAP, aggregate_byte_cap=BLOB_INLINE_AGGREGATE_BYTE_CAP
    )
    assert violations == []


def test_one_byte_over_the_cap_is_refused_naming_total_cap_and_count() -> None:
    sizes = [*_sizes_totalling(BLOB_INLINE_AGGREGATE_BYTE_CAP), 1]
    refs, records, config = _distinct_files(sizes)
    expected = _total_refusal(BLOB_INLINE_AGGREGATE_BYTE_CAP + 1, BLOB_INLINE_AGGREGATE_BYTE_CAP, len(sizes))

    violations = _validate_blob_content_refs_sync(
        records.get, config, per_ref_byte_cap=BLOB_INLINE_PER_REF_BYTE_CAP, aggregate_byte_cap=BLOB_INLINE_AGGREGATE_BYTE_CAP
    )
    assert [(v.category, v.field_path, v.detail) for v in violations] == [("oversized", AGGREGATE_PATH, expected)]

    with pytest.raises(BlobContentResolutionError) as refusal:
        _enforce_blob_content_ref_metadata(
            refs, records, per_ref_byte_cap=BLOB_INLINE_PER_REF_BYTE_CAP, aggregate_byte_cap=BLOB_INLINE_AGGREGATE_BYTE_CAP
        )
    assert refusal.value.oversized == ((AGGREGATE_PATH, BLOB_INLINE_AGGREGATE_BYTE_CAP + 1, BLOB_INLINE_AGGREGATE_BYTE_CAP),)
    assert refusal.value.aggregate_binding_count == len(sizes)
    assert expected in str(refusal.value)


def test_refusal_text_names_total_cap_and_count() -> None:
    from elspeth.contracts.blobs_inline import BLOB_INLINE_AGGREGATE_FIELD_PATH, inline_blob_total_refusal

    assert BLOB_INLINE_AGGREGATE_FIELD_PATH == AGGREGATE_PATH
    assert inline_blob_total_refusal(4_194_305, 4_194_304, 17) == _total_refusal(4_194_305, 4_194_304, 17)
    text = inline_blob_total_refusal(4_194_305, 4_194_304, 17)
    assert text == (
        "File-held text totals 4194305 bytes across 17 file bindings; the limit is 4194304 bytes. "
        "A file bound in more than one place counts once per place. Remove a binding or shorten the largest files."
    )


@pytest.mark.parametrize(
    ("bindings", "admitted"),
    [
        pytest.param(BLOB_INLINE_AGGREGATE_BYTE_CAP // BLOB_INLINE_PER_REF_BYTE_CAP, True, id="bound-cap-over-file-size-times"),
        pytest.param(BLOB_INLINE_AGGREGATE_BYTE_CAP // BLOB_INLINE_PER_REF_BYTE_CAP + 1, False, id="one-more-binding"),
    ],
)
def test_one_file_bound_in_many_places_counts_once_per_binding(bindings: int, admitted: bool) -> None:
    blob_id = uuid4()
    digest = "b" * 64
    record = _record(blob_id, BLOB_INLINE_PER_REF_BYTE_CAP, digest)
    refs = [
        BlobInlineRef(blob_id=blob_id, sha256=digest, field_path=f"source.options.file_{index}", encoding="utf-8")
        for index in range(bindings)
    ]
    config = {
        "source": {
            "options": {
                f"file_{index}": {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": digest} for index in range(bindings)
            }
        }
    }

    violations = _validate_blob_content_refs_sync(
        {blob_id: record}.get,
        config,
        per_ref_byte_cap=BLOB_INLINE_PER_REF_BYTE_CAP,
        aggregate_byte_cap=BLOB_INLINE_AGGREGATE_BYTE_CAP,
    )
    if admitted:
        assert violations == []
        return
    total = bindings * BLOB_INLINE_PER_REF_BYTE_CAP
    assert [v.detail for v in violations] == [_total_refusal(total, BLOB_INLINE_AGGREGATE_BYTE_CAP, bindings)]
    with pytest.raises(BlobContentResolutionError) as refusal:
        _enforce_blob_content_ref_metadata(
            refs,
            {blob_id: record},
            per_ref_byte_cap=BLOB_INLINE_PER_REF_BYTE_CAP,
            aggregate_byte_cap=BLOB_INLINE_AGGREGATE_BYTE_CAP,
        )
    assert refusal.value.aggregate_binding_count == bindings


@pytest.mark.parametrize(
    ("size", "refused"),
    [
        pytest.param(BLOB_INLINE_PER_REF_BYTE_CAP, False, id="exactly-per-file-cap"),
        pytest.param(BLOB_INLINE_PER_REF_BYTE_CAP + 1, True, id="per-file-cap-plus-one"),
    ],
)
def test_per_file_cap_is_unchanged(size: int, refused: bool) -> None:
    refs, records, config = _distinct_files([size])

    violations = _validate_blob_content_refs_sync(
        records.get, config, per_ref_byte_cap=BLOB_INLINE_PER_REF_BYTE_CAP, aggregate_byte_cap=BLOB_INLINE_AGGREGATE_BYTE_CAP
    )

    assert [(v.category, v.detail) for v in violations] == (
        [("oversized", f"{size} bytes exceeds per-ref cap {BLOB_INLINE_PER_REF_BYTE_CAP}")] if refused else []
    )
    if not refused:
        _enforce_blob_content_ref_metadata(
            refs, records, per_ref_byte_cap=BLOB_INLINE_PER_REF_BYTE_CAP, aggregate_byte_cap=BLOB_INLINE_AGGREGATE_BYTE_CAP
        )
        return
    with pytest.raises(BlobContentResolutionError) as refusal:
        _enforce_blob_content_ref_metadata(
            refs, records, per_ref_byte_cap=BLOB_INLINE_PER_REF_BYTE_CAP, aggregate_byte_cap=BLOB_INLINE_AGGREGATE_BYTE_CAP
        )
    assert refusal.value.oversized == ((refs[0].field_path, size, BLOB_INLINE_PER_REF_BYTE_CAP),)
    assert refusal.value.aggregate_binding_count is None


def test_summary_without_a_total_refusal_is_unchanged() -> None:
    error = BlobContentResolutionError(oversized=[("source.options.x", 10, 5)], missing=["source.options.y"])
    assert str(error) == "Blob inline content resolution failed: missing=1, oversized=1"
    assert error.aggregate_binding_count is None
