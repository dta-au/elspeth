"""Tests for ``elspeth.web.execution.preview.build_artifact_preview``.

These exercise the text/binary classifier and the
byte-cap/row-cap interplay independently of FastAPI plumbing.
The endpoint-level tests live in ``test_outputs_routes.py``.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from elspeth.web.execution.preview import (
    _classify_text_or_binary,
    build_artifact_preview,
)


def _write_llm_csv(
    path: Path,
    *,
    record_count: int,
    answer_lines: int,
    delimiter: str = ",",
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("id", "question", "answer", "answer_usage"),
            delimiter=delimiter,
        )
        writer.writeheader()
        for index in range(record_count):
            writer.writerow(
                {
                    "id": index,
                    "question": f"Question {index}?",
                    "answer": "\n".join(f"answer {index} line {line_number}" for line_number in range(answer_lines)),
                    "answer_usage": {
                        "completion_tokens": 60,
                        "prompt_tokens": 100,
                        "total_tokens": 160,
                    },
                }
            )


class TestClassifyTextOrBinary:
    def test_pure_ascii_is_text(self) -> None:
        is_text, decoded = _classify_text_or_binary(b"hello\nworld\n")
        assert is_text is True
        assert decoded == "hello\nworld\n"

    def test_complete_utf8_multibyte_is_text(self) -> None:
        # "héllo wörld" — multi-byte chars throughout, decodes cleanly.
        payload = "héllo wörld\n".encode()
        is_text, decoded = _classify_text_or_binary(payload)
        assert is_text is True
        assert decoded == "héllo wörld\n"

    def test_truncated_utf8_codepoint_in_tail_is_still_text(self) -> None:
        # Four-byte emoji '🦀' (\xf0\x9f\xa6\x80) sliced after first 2 bytes:
        # the trailing partial sequence is in the last 3 bytes — must be
        # treated as text via errors='ignore', not flipped to binary.
        full = "abc🦀def".encode()
        # Cut so the emoji is partial at the END of the buffer
        truncated = full[: full.index(b"\xf0") + 2]  # keep first 2 bytes of the emoji
        is_text, decoded = _classify_text_or_binary(truncated)
        assert is_text is True
        # 'abc' survives; the partial emoji bytes are dropped.
        assert decoded == "abc"

    def test_genuinely_binary_bytes_are_binary(self) -> None:
        # JPEG SOI marker followed by APP0 — invalid UTF-8 from the very start.
        payload = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 100
        is_text, decoded = _classify_text_or_binary(payload)
        assert is_text is False
        assert decoded == ""

    def test_binary_byte_in_middle_is_binary_not_text(self) -> None:
        # Lone continuation byte (0x80) far from the tail — real binary.
        payload = b"text " + b"\x80" + b" more text plus padding " + b"x" * 100
        is_text, decoded = _classify_text_or_binary(payload)
        assert is_text is False
        assert decoded == ""

    def test_empty_buffer_is_text(self) -> None:
        is_text, decoded = _classify_text_or_binary(b"")
        assert is_text is True
        assert decoded == ""


class TestBuildArtifactPreview:
    def test_csv_under_caps_returns_full_content(self, tmp_path: Path) -> None:
        f = tmp_path / "small.csv"
        f.write_text("col1,col2\n1,2\n3,4\n")

        preview = build_artifact_preview(f, artifact_id="art-1")

        assert preview.content_type == "csv"
        assert preview.preview_text == "col1,col2\n1,2\n3,4\n"
        assert preview.truncated is False
        assert preview.total_size_bytes == f.stat().st_size
        assert preview.row_count_preview == 3  # header + 2 data rows

    def test_csv_over_row_cap_truncates_to_row_cap(self, tmp_path: Path) -> None:
        f = tmp_path / "many_rows.csv"
        # 200 rows of "x" — well under the byte cap, but over the row cap.
        f.write_text("\n".join(f"row{i}" for i in range(200)) + "\n")

        preview = build_artifact_preview(f, artifact_id="art-2", row_cap=50)

        assert preview.content_type == "csv"
        assert preview.row_count_preview == 50
        assert preview.truncated is True
        assert preview.preview_text.count("\n") == 50  # 50 complete records

    @pytest.mark.parametrize(
        ("record_count", "answer_lines"),
        ((30, 6), (5, 120)),
    )
    def test_csv_multiline_llm_records_are_capped_as_logical_records(
        self,
        tmp_path: Path,
        record_count: int,
        answer_lines: int,
    ) -> None:
        f = tmp_path / f"llm_{record_count}x{answer_lines}.csv"
        _write_llm_csv(
            f,
            record_count=record_count,
            answer_lines=answer_lines,
        )

        preview = build_artifact_preview(f, artifact_id="art-multiline")

        parsed_rows = list(csv.reader(io.StringIO(preview.preview_text), strict=True))
        assert len(parsed_rows) == record_count + 1  # header plus every data record
        assert preview.row_count_preview == len(parsed_rows)
        assert preview.preview_text == f.read_bytes().decode("utf-8")
        assert preview.truncated is False

    def test_csv_row_cap_counts_quoted_multiline_records(self, tmp_path: Path) -> None:
        f = tmp_path / "multiline_row_cap.csv"
        _write_llm_csv(f, record_count=8, answer_lines=3)

        preview = build_artifact_preview(
            f,
            artifact_id="art-logical-row-cap",
            row_cap=4,
        )

        parsed_rows = list(csv.reader(io.StringIO(preview.preview_text), strict=True))
        assert len(parsed_rows) == 4  # header plus three complete data records
        assert preview.row_count_preview == len(parsed_rows)
        assert preview.truncated is True

    @pytest.mark.parametrize("partial_value", ('"line one\nline two', "partial-unquoted"))
    def test_csv_byte_cap_omits_the_partial_final_record(
        self,
        tmp_path: Path,
        partial_value: str,
    ) -> None:
        f = tmp_path / "byte_capped.csv"
        complete_prefix = "id,value\r\n1,complete\r\n"
        content = complete_prefix + f"2,{partial_value}\r\n3,unseen\r\n"
        f.write_bytes(content.encode("utf-8"))
        byte_cap = len((complete_prefix + f"2,{partial_value}").encode("utf-8"))

        preview = build_artifact_preview(
            f,
            artifact_id="art-byte-cap",
            byte_cap=byte_cap,
        )

        assert preview.preview_text == complete_prefix
        assert preview.row_count_preview == 2
        assert len(list(csv.reader(io.StringIO(preview.preview_text), strict=True))) == 2
        assert preview.truncated is True

    def test_malformed_csv_degrades_to_raw_text_without_a_row_count(self, tmp_path: Path) -> None:
        f = tmp_path / "malformed.csv"
        content = 'id,note\n1,"unterminated\nstill open'
        f.write_text(content, encoding="utf-8")

        preview = build_artifact_preview(f, artifact_id="art-malformed")

        assert preview.preview_text == content
        assert preview.row_count_preview is None
        assert preview.truncated is False

    def test_tsv_row_cap_honours_quoted_multiline_records(self, tmp_path: Path) -> None:
        f = tmp_path / "multiline.tsv"
        _write_llm_csv(f, record_count=3, answer_lines=4, delimiter="\t")

        preview = build_artifact_preview(
            f,
            artifact_id="art-tsv",
            row_cap=2,
        )

        parsed_rows = list(csv.reader(io.StringIO(preview.preview_text), delimiter="\t", strict=True))
        assert len(parsed_rows) == 2
        assert preview.row_count_preview == len(parsed_rows)
        assert preview.truncated is True

    def test_text_under_byte_cap_returns_full_content(self, tmp_path: Path) -> None:
        f = tmp_path / "log.txt"
        f.write_text("line one\nline two\n")

        preview = build_artifact_preview(f, artifact_id="art-3")

        assert preview.content_type == "text"
        assert preview.preview_text == "line one\nline two\n"
        assert preview.truncated is False
        assert preview.row_count_preview is None  # not tabular

    def test_text_over_byte_cap_marks_truncated(self, tmp_path: Path) -> None:
        f = tmp_path / "big.txt"
        # Use bytes well beyond the 1 KiB cap.
        f.write_bytes(b"a" * 5000)

        preview = build_artifact_preview(f, artifact_id="art-4", byte_cap=1024)

        assert preview.content_type == "text"
        assert preview.truncated is True
        assert preview.total_size_bytes == 5000
        assert len(preview.preview_text) <= 1024

    def test_binary_extension_returns_binary_with_no_preview(self, tmp_path: Path) -> None:
        f = tmp_path / "image.bin"
        f.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 200)

        preview = build_artifact_preview(f, artifact_id="art-5")

        assert preview.content_type == "binary"
        assert preview.preview_text == ""
        assert preview.row_count_preview is None

    def test_extension_says_csv_but_bytes_are_binary(self, tmp_path: Path) -> None:
        # Defence: a sink mis-extensioned its output. Binary detection
        # must override the format hint from extension.
        f = tmp_path / "lying.csv"
        f.write_bytes(b"\xff\xd8" + b"\x80\x80\x80" + b"row1\n" + b"x" * 200)

        preview = build_artifact_preview(f, artifact_id="art-6")

        assert preview.content_type == "binary"
        assert preview.preview_text == ""

    def test_jsonl_extension_uses_jsonl_content_type(self, tmp_path: Path) -> None:
        f = tmp_path / "events.jsonl"
        f.write_text('{"a":1}\n{"a":2}\n')

        preview = build_artifact_preview(f, artifact_id="art-7")

        assert preview.content_type == "jsonl"
        assert preview.row_count_preview == 2

    def test_jsonl_byte_cap_omits_the_partial_final_record(self, tmp_path: Path) -> None:
        f = tmp_path / "byte_capped.jsonl"
        complete_prefix = '{"id":1}\n'
        content = complete_prefix + '{"id":2,"value":"partial"}\n{"id":3}\n'
        f.write_text(content, encoding="utf-8")
        byte_cap = len((complete_prefix + '{"id":2,"value":"part').encode("utf-8"))

        preview = build_artifact_preview(
            f,
            artifact_id="art-jsonl-byte-cap",
            byte_cap=byte_cap,
        )

        assert preview.preview_text == complete_prefix
        assert preview.row_count_preview == 1
        assert preview.truncated is True

    def test_jsonl_row_cap_counts_nonempty_logical_records(self, tmp_path: Path) -> None:
        f = tmp_path / "blank_lines.jsonl"
        f.write_text('{"id":1}\n\n{"id":2}\n{"id":3}\n', encoding="utf-8")

        preview = build_artifact_preview(
            f,
            artifact_id="art-jsonl-row-cap",
            row_cap=2,
        )

        assert preview.preview_text == '{"id":1}\n\n{"id":2}\n'
        assert preview.row_count_preview == 2
        assert preview.truncated is True

    def test_jsonl_crlf_blank_frames_are_not_counted(self, tmp_path: Path) -> None:
        f = tmp_path / "crlf_blank_lines.jsonl"
        content = b'{"id":1}\r\n\r\n{"id":2}\r\n'
        f.write_bytes(content)

        preview = build_artifact_preview(f, artifact_id="art-jsonl-crlf")

        assert preview.preview_text == content.decode("utf-8")
        assert preview.row_count_preview == 2
        assert preview.truncated is False

    def test_json_extension_uses_json_content_type(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.json"
        f.write_text('{"key": "value"}')

        preview = build_artifact_preview(f, artifact_id="art-8")

        assert preview.content_type == "json"
        assert preview.row_count_preview is None

    def test_unknown_extension_with_text_bytes_falls_back_to_text(self, tmp_path: Path) -> None:
        f = tmp_path / "what.xyz"
        f.write_text("hello\n")

        preview = build_artifact_preview(f, artifact_id="art-9")

        assert preview.content_type == "text"
        assert preview.preview_text == "hello\n"

    def test_csv_with_byte_cap_below_row_cap_marks_truncated(self, tmp_path: Path) -> None:
        # The byte cap fires before the row cap: even though we have
        # fewer than row_cap complete records, the final record may itself
        # be cut and withheld. Truncated must be True.
        f = tmp_path / "small_rows_huge_cells.csv"
        f.write_text("a,b\n" + ("x" * 100 + ",y\n") * 20)  # ~2 KiB

        preview = build_artifact_preview(f, artifact_id="art-10", byte_cap=200)

        assert preview.truncated is True
        assert preview.total_size_bytes == f.stat().st_size

    def test_total_size_bytes_uses_live_filesystem_size(self, tmp_path: Path) -> None:
        # build_artifact_preview reads stat() at call time — caller can
        # cross-check against the manifest's recorded size.
        f = tmp_path / "out.txt"
        f.write_bytes(b"a" * 42)
        preview = build_artifact_preview(f, artifact_id="art-11")
        assert preview.total_size_bytes == 42
