"""Bounded artifact-preview reader.

Companion to ``elspeth.web.execution.outputs``: where ``outputs``
returns the audit-evidence manifest, this module reads a head-of-file
preview so the operator UI can show the first N rows / first N bytes
of a sink-write artefact without forcing a full download.

Bounded reads are the explicit design:
* ``_DEFAULT_BYTE_CAP = 256 KiB`` — cap on bytes read from disk per
  request. Caps memory and IO blast radius.
* ``_DEFAULT_ROW_CAP = 100`` — for tabular formats (``.csv``, ``.tsv``,
  ``.jsonl``), the additional row-count cap below the byte cap.

UTF-8 truncation discipline
---------------------------
A naive "read N bytes, then ``decode('utf-8')``" would routinely raise
on legitimate text files: any multi-byte codepoint sliced by the byte
cap looks like a malformed sequence. Treating that as "binary" would
be wrong (the file is perfectly good UTF-8; the cap landed mid-codepoint).

We classify text vs binary with a probe:

* If ``head_bytes.decode('utf-8', errors='strict')`` succeeds → text.
* Else, if the failure position is within the **last 3 bytes** of the
  buffer → it's a truncation artifact (UTF-8 codepoints are at most 4
  bytes; any partial cut is in the tail). Re-decode with
  ``errors='ignore'`` to drop the partial sequence and treat as text.
* Else → genuine binary (a malformed sequence appears far from the cap
  boundary, e.g., a JPEG header byte at offset 0).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from elspeth.web.execution.schemas import RunOutputArtifactPreview

_DEFAULT_BYTE_CAP = 256 * 1024
_DEFAULT_ROW_CAP = 100
DEFAULT_ARTIFACT_PREVIEW_BYTE_CAP = _DEFAULT_BYTE_CAP

# Extensions we render as a parsed-row table on the frontend.
_CSV_EXTENSIONS = frozenset({".csv", ".tsv"})
_JSONL_EXTENSIONS = frozenset({".jsonl", ".ndjson"})
# Extensions we render as monospace text but not a table.
_JSON_EXTENSIONS = frozenset({".json"})
_PLAIN_TEXT_EXTENSIONS = frozenset(
    {".txt", ".log", ".md", ".markdown", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sql", ".html", ".htm", ".xml", ".tsv"}
)

PreviewContentType = Literal["text", "csv", "jsonl", "json", "binary"]


def _csv_record_end_offsets(text: str, *, delimiter: str) -> tuple[list[int], bool]:
    """Return complete CSV record boundaries and whether EOF is quoted.

    This is the boundary-only counterpart of the frontend's
    ``parseCsvRows`` reader. It never materialises fields or rows: the input
    is already byte-bounded, and the only facts this layer needs are where a
    complete logical record ends and whether the bounded head stopped inside
    a quoted field.
    """
    record_ends: list[int] = []
    in_quotes = False
    field_start = True
    index = 0
    while index < len(text):
        char = text[index]
        if in_quotes:
            if char == '"':
                if index + 1 < len(text) and text[index + 1] == '"':
                    index += 1
                else:
                    in_quotes = False
            index += 1
            continue
        if char == '"' and field_start:
            in_quotes = True
        elif char == delimiter:
            field_start = True
        elif char == "\n":
            record_ends.append(index + 1)
            field_start = True
        elif char != "\r":
            field_start = False
        index += 1

    if not in_quotes and text and (not record_ends or record_ends[-1] < len(text)):
        record_ends.append(len(text))
    return record_ends, in_quotes


def _raw_tabular_fallback(
    head_text: str,
    *,
    row_cap: int,
    truncated_by_bytes: bool,
) -> tuple[str, None, bool]:
    """Keep malformed tabular data inspectable without claiming a row count."""
    lines = head_text.splitlines()
    if len(lines) > row_cap:
        return "\n".join(lines[:row_cap]), None, True
    return head_text, None, truncated_by_bytes


def _build_csv_preview(
    head_text: str,
    *,
    delimiter: str,
    row_cap: int,
    truncated_by_bytes: bool,
) -> tuple[str, int | None, bool]:
    """Bound CSV/TSV by complete logical records, never physical lines."""
    record_ends, ended_in_quotes = _csv_record_end_offsets(
        head_text,
        delimiter=delimiter,
    )
    if ended_in_quotes and not truncated_by_bytes:
        return _raw_tabular_fallback(
            head_text,
            row_cap=row_cap,
            truncated_by_bytes=False,
        )

    # A byte-bounded head that does not end at a line boundary may contain a
    # syntactically plausible but incomplete unquoted record. Do not count or
    # publish that fragment. The quoted equivalent is already absent because
    # it has no record-end offset.
    if truncated_by_bytes and head_text and not head_text.endswith(("\n", "\r")) and record_ends and record_ends[-1] == len(head_text):
        record_ends.pop()

    selected_ends = record_ends[:row_cap]
    preview_text = head_text[: selected_ends[-1]] if selected_ends else ""
    truncated_by_rows = len(record_ends) > row_cap
    return preview_text, len(selected_ends), truncated_by_bytes or truncated_by_rows


def _build_jsonl_preview(
    head_text: str,
    *,
    row_cap: int,
    truncated_by_bytes: bool,
) -> tuple[str, int, bool]:
    """Bound JSONL by complete, non-empty records as the renderer does."""
    record_ends: list[int] = []
    offset = 0
    frames = head_text.splitlines(keepends=True)
    for frame_index, frame in enumerate(frames):
        offset += len(frame)
        is_partial_tail = truncated_by_bytes and frame_index == len(frames) - 1 and not frame.endswith(("\n", "\r"))
        if is_partial_tail:
            continue
        if frame.rstrip("\r\n") != "":
            record_ends.append(offset)

    selected_ends = record_ends[:row_cap]
    truncated_by_rows = len(record_ends) > row_cap
    if truncated_by_bytes or truncated_by_rows:
        preview_text = head_text[: selected_ends[-1]] if selected_ends else ""
    else:
        preview_text = head_text
    return preview_text, len(selected_ends), truncated_by_bytes or truncated_by_rows


def _classify_text_or_binary(head_bytes: bytes) -> tuple[bool, str]:
    """Return ``(is_text, decoded_text)``.

    A trailing partial UTF-8 sequence (cut by the byte cap) is treated
    as text — silently dropped via ``errors='ignore'``. A malformed
    sequence anywhere else is treated as genuine binary.
    """
    try:
        return True, head_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        # UTF-8 codepoints are at most 4 bytes. A truncation artifact
        # always lies within the last 3 bytes of the buffer.
        if exc.start >= len(head_bytes) - 3:
            return True, head_bytes.decode("utf-8", errors="ignore")
        return False, ""


def _select_content_type(suffix: str) -> PreviewContentType:
    suffix = suffix.lower()
    if suffix in _CSV_EXTENSIONS:
        return "csv"
    if suffix in _JSONL_EXTENSIONS:
        return "jsonl"
    if suffix in _JSON_EXTENSIONS:
        return "json"
    if suffix in _PLAIN_TEXT_EXTENSIONS:
        return "text"
    # Unknown extension but UTF-8 decodable: still serve as text.
    return "text"


def build_artifact_preview(
    fs_path: Path,
    *,
    artifact_id: str,
    byte_cap: int = _DEFAULT_BYTE_CAP,
    row_cap: int = _DEFAULT_ROW_CAP,
) -> RunOutputArtifactPreview:
    """Build a bounded preview for ``fs_path``.

    Caller is responsible for verifying ``fs_path`` is in the sink
    allowlist and exists. This function reads at most ``byte_cap`` bytes
    and, for tabular content types, additionally caps at ``row_cap`` rows.
    """
    total_size_bytes = fs_path.stat().st_size
    with fs_path.open("rb") as f:
        head_bytes = f.read(byte_cap)
    return _build_artifact_preview_from_head(
        fs_path,
        artifact_id=artifact_id,
        total_size_bytes=total_size_bytes,
        head_bytes=head_bytes,
        byte_cap=byte_cap,
        row_cap=row_cap,
    )


def build_artifact_preview_from_head(
    fs_path: Path,
    *,
    artifact_id: str,
    total_size_bytes: int,
    head_bytes: bytes,
    byte_cap: int = _DEFAULT_BYTE_CAP,
    row_cap: int = _DEFAULT_ROW_CAP,
) -> RunOutputArtifactPreview:
    """Build a bounded preview from a previously verified head-of-file snapshot."""
    return _build_artifact_preview_from_head(
        fs_path,
        artifact_id=artifact_id,
        total_size_bytes=total_size_bytes,
        head_bytes=head_bytes,
        byte_cap=byte_cap,
        row_cap=row_cap,
    )


def _build_artifact_preview_from_head(
    fs_path: Path,
    *,
    artifact_id: str,
    total_size_bytes: int,
    head_bytes: bytes,
    byte_cap: int,
    row_cap: int,
) -> RunOutputArtifactPreview:
    bytes_read = len(head_bytes)
    truncated_by_bytes = bytes_read < total_size_bytes

    is_text, head_text = _classify_text_or_binary(head_bytes)
    if not is_text:
        return RunOutputArtifactPreview(
            artifact_id=artifact_id,
            content_type="binary",
            preview_text="",
            truncated=truncated_by_bytes,
            total_size_bytes=total_size_bytes,
            row_count_preview=None,
        )

    content_type = _select_content_type(fs_path.suffix)

    if content_type == "csv":
        delimiter = "\t" if fs_path.suffix.lower() == ".tsv" else ","
        preview_text, row_count_preview, truncated = _build_csv_preview(
            head_text,
            delimiter=delimiter,
            row_cap=row_cap,
            truncated_by_bytes=truncated_by_bytes,
        )
    elif content_type == "jsonl":
        preview_text, row_count_preview, truncated = _build_jsonl_preview(
            head_text,
            row_cap=row_cap,
            truncated_by_bytes=truncated_by_bytes,
        )
    else:
        preview_text = head_text
        row_count_preview = None
        truncated = truncated_by_bytes

    return RunOutputArtifactPreview(
        artifact_id=artifact_id,
        content_type=content_type,
        preview_text=preview_text,
        truncated=truncated,
        total_size_bytes=total_size_bytes,
        row_count_preview=row_count_preview,
    )
