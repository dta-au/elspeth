"""The session snapshot (YAML export, prompt review) applies the same per-binding total as validation.

Before 0.8.16 the snapshot counted each FILE once while validation and run admission
counted each BINDING; a file bound in two places counted once here and twice there.
It now counts bindings too. That can only refuse here what validation already
refuses, because the per-binding total is never below the per-file total.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.blobs import BlobRecord, InlineBlobNotAdmittedError
from elspeth.contracts.enums import CreationModality
from elspeth.core.blobs_inline import BLOB_INLINE_AGGREGATE_BYTE_CAP, BLOB_INLINE_PER_REF_BYTE_CAP
from elspeth.web.sessions.inline_blob_preflight import (
    InlinePreflightState,
    SessionInlineBlobSnapshot,
    prepare_session_inline_blob_snapshot,
)

OLD_AGGREGATE_CAP = 1024 * 1024
FILES_AT_CAP = BLOB_INLINE_AGGREGATE_BYTE_CAP // BLOB_INLINE_PER_REF_BYTE_CAP


def _record(blob_id: UUID, session_id: UUID, content: bytes) -> BlobRecord:
    return BlobRecord(
        id=blob_id,
        session_id=session_id,
        filename="prompt.txt",
        mime_type="text/plain",
        size_bytes=len(content),
        content_hash=hashlib.sha256(content).hexdigest(),
        storage_path="inline-prompt.txt",
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


def _snapshot(bindings: list[tuple[UUID, bytes]], *, with_hint: bool) -> tuple[SessionInlineBlobSnapshot, list[UUID]]:
    """Bind each (blob, content) pair at its own node, in order; return the snapshot and the blobs read."""
    session_id = uuid4()
    contents = dict(bindings)
    nodes = [
        {
            "id": f"n{index}",
            "options": {
                "prompt_template": {
                    "blob_ref": str(blob_id),
                    "mode": "inline_content",
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            },
        }
        for index, (blob_id, content) in enumerate(bindings)
    ]
    reads: list[UUID] = []

    def read_blob(blob_id: UUID) -> tuple[BlobRecord, bytes]:
        reads.append(blob_id)
        return _record(blob_id, session_id, contents[blob_id]), contents[blob_id]

    def metadata_hint(blob_id: UUID) -> tuple[str, str | None, int]:
        return "ready", hashlib.sha256(contents[blob_id]).hexdigest(), len(contents[blob_id])

    snapshot = prepare_session_inline_blob_snapshot(
        InlinePreflightState(sources={}, nodes=nodes, outputs=[]),
        session_id=session_id,
        read_blob=read_blob,
        metadata_hint=metadata_hint if with_hint else None,
    )
    assert snapshot is not None
    return snapshot, reads


def _distinct(sizes: list[int]) -> list[tuple[UUID, bytes]]:
    return [(uuid4(), bytes([65 + index % 26]) * size) for index, size in enumerate(sizes)]


def _sizes_totalling(total: int) -> list[int]:
    full, remainder = divmod(total, BLOB_INLINE_PER_REF_BYTE_CAP)
    return [BLOB_INLINE_PER_REF_BYTE_CAP] * full + ([remainder] if remainder else [])


@pytest.mark.parametrize("with_hint", [False, True], ids=["no-hint", "metadata-hint"])
@pytest.mark.parametrize(
    "total",
    [
        pytest.param(OLD_AGGREGATE_CAP + 1, id="old-cap-plus-one"),
        pytest.param(BLOB_INLINE_AGGREGATE_BYTE_CAP, id="exactly-cap"),
    ],
)
def test_totals_up_to_the_cap_are_all_admitted(total: int, with_hint: bool) -> None:
    bindings = _distinct(_sizes_totalling(total))

    snapshot, _reads = _snapshot(bindings, with_hint=with_hint)

    assert snapshot.refusals == {}
    for blob_id, content in bindings:
        assert snapshot.content(blob_id)[1] == content


@pytest.mark.parametrize("with_hint", [False, True], ids=["no-hint", "metadata-hint"])
def test_one_byte_over_the_cap_refuses_the_file_that_crosses_it(with_hint: bool) -> None:
    bindings = _distinct([*_sizes_totalling(BLOB_INLINE_AGGREGATE_BYTE_CAP), 1])

    snapshot, reads = _snapshot(bindings, with_hint=with_hint)

    crossing, _content = bindings[-1]
    assert dict(snapshot.refusals) == {crossing: "aggregate_oversized"}
    # A metadata hint refuses the crossing file from its declared size, before reading it.
    assert (crossing in reads) is not with_hint
    with pytest.raises(InlineBlobNotAdmittedError) as refusal:
        snapshot.content(crossing)
    assert (refusal.value.reason, refusal.value.limit_bytes) == ("aggregate_oversized", BLOB_INLINE_AGGREGATE_BYTE_CAP)


@pytest.mark.parametrize("with_hint", [False, True], ids=["no-hint", "metadata-hint"])
def test_one_file_bound_up_to_the_cap_is_read_once_and_admitted(with_hint: bool) -> None:
    blob = (uuid4(), b"r" * BLOB_INLINE_PER_REF_BYTE_CAP)

    snapshot, reads = _snapshot([blob] * FILES_AT_CAP, with_hint=with_hint)

    assert reads == [blob[0]]
    assert snapshot.refusals == {}
    assert snapshot.content(blob[0])[1] == blob[1]


@pytest.mark.parametrize("with_hint", [False, True], ids=["no-hint", "metadata-hint"])
def test_one_file_bound_once_more_than_the_cap_holds_is_refused(with_hint: bool) -> None:
    blob = (uuid4(), b"r" * BLOB_INLINE_PER_REF_BYTE_CAP)

    snapshot, reads = _snapshot([blob] * (FILES_AT_CAP + 1), with_hint=with_hint)

    assert reads == [blob[0]]
    assert dict(snapshot.refusals) == {blob[0]: "aggregate_oversized"}
    assert blob[0] not in snapshot.records
    with pytest.raises(InlineBlobNotAdmittedError) as refusal:
        snapshot.content(blob[0])
    assert refusal.value.reason == "aggregate_oversized"


def test_a_repeated_binding_counts_toward_files_bound_after_it() -> None:
    """File A bound FILES_AT_CAP times fills the total exactly, so B crosses it although A is read once."""
    file_a = (uuid4(), b"a" * BLOB_INLINE_PER_REF_BYTE_CAP)
    file_b = (uuid4(), b"b" * 2)

    snapshot, reads = _snapshot([file_a] * FILES_AT_CAP + [file_b], with_hint=False)

    assert reads == [file_a[0], file_b[0]]
    assert dict(snapshot.refusals) == {file_b[0]: "aggregate_oversized"}
    assert snapshot.content(file_a[0])[1] == file_a[1]


def test_prompt_review_card_names_the_new_cap() -> None:
    """The card a total refusal becomes (coordination repository) names the cap, not the total or count."""
    from elspeth.web.sessions.protocol import PromptFileTooLargeError

    blob = (uuid4(), b"r" * BLOB_INLINE_PER_REF_BYTE_CAP)
    snapshot, _reads = _snapshot([blob] * (FILES_AT_CAP + 1), with_hint=False)
    with pytest.raises(InlineBlobNotAdmittedError) as refusal:
        snapshot.content(blob[0])
    assert refusal.value.limit_bytes is not None

    card = PromptFileTooLargeError(
        node_id="n0", limit_bytes=refusal.value.limit_bytes, aggregate=refusal.value.reason == "aggregate_oversized"
    )

    assert card.error_code == "prompt_file_too_large"
    assert str(card) == (
        "Node n0: The inline files bound to this pipeline together exceed the 4194304-byte (4096 KiB) inline limit, "
        "so a run could not use it. Shorten the prompt file, then review it."
    )


@pytest.mark.parametrize(
    ("size", "refused"),
    [
        pytest.param(BLOB_INLINE_PER_REF_BYTE_CAP, False, id="exactly-per-file-cap"),
        pytest.param(BLOB_INLINE_PER_REF_BYTE_CAP + 1, True, id="per-file-cap-plus-one"),
    ],
)
def test_per_file_cap_is_unchanged(size: int, refused: bool) -> None:
    ((blob_id, _content),) = bindings = _distinct([size])

    snapshot, _reads = _snapshot(bindings, with_hint=False)

    assert dict(snapshot.refusals) == ({blob_id: "oversized"} if refused else {})
