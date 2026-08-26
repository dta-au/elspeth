"""Tests for the blob_text_expand transform."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts import Determinism
from elspeth.contracts.payload_store import IntegrityError, PayloadNotFoundError
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.blob_text_expand import BlobTextExpand, _split_bounded
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context

DYNAMIC_SCHEMA = {"mode": "observed", "guaranteed_fields": ["url", "blob_ref"]}


class _PayloadStoreFake:
    def __init__(self, content_by_hash: dict[str, bytes]) -> None:
        self.content_by_hash = content_by_hash

    def store(self, content: bytes) -> str:
        content_hash = _hash(content)
        self.content_by_hash[content_hash] = content
        return content_hash

    def retrieve(self, content_hash: str) -> bytes:
        if content_hash not in self.content_by_hash:
            raise PayloadNotFoundError(content_hash)
        return self.content_by_hash[content_hash]

    def exists(self, content_hash: str) -> bool:
        return content_hash in self.content_by_hash

    def delete(self, content_hash: str) -> bool:
        return self.content_by_hash.pop(content_hash, None) is not None


class _IntegrityFailingStore:
    def retrieve(self, content_hash: str) -> bytes:
        raise IntegrityError(f"content hash mismatch for {content_hash}")


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _build_transform(**overrides: Any) -> BlobTextExpand:
    config: dict[str, Any] = {"schema": DYNAMIC_SCHEMA, "blob_ref_field": "blob_ref"}
    config.update(overrides)
    return BlobTextExpand(config)


def _run(body: bytes, *, row: dict[str, Any] | None = None, **overrides: Any) -> tuple[BlobTextExpand, Any]:
    blob_ref = _hash(body)
    transform = _build_transform(**overrides)
    transform._payload_store = _PayloadStoreFake({blob_ref: body})
    row_data = {"url": "https://example.test/a.txt", "blob_ref": blob_ref}
    if row is not None:
        row_data.update(row)
    return transform, transform.process(make_pipeline_row(row_data), make_context())


# --------------------------------------------------------------------------
# Happy path: what the transform emits, and what it keeps.
# --------------------------------------------------------------------------


def test_emits_one_row_per_line_and_preserves_upstream_fields() -> None:
    """PROVES the 1->N contract: line text, source index, and every input field."""
    body = b"alpha\nbeta\ngamma\n"
    transform, result = _run(body, row={"manifest_index": 7})

    assert result.status == "success"
    assert result.is_multi_row
    assert result.rows is not None
    blob_ref = _hash(body)
    assert [row.to_dict() for row in result.rows] == [
        {"url": "https://example.test/a.txt", "blob_ref": blob_ref, "manifest_index": 7, "line": "alpha", "line_index": 0},
        {"url": "https://example.test/a.txt", "blob_ref": blob_ref, "manifest_index": 7, "line": "beta", "line_index": 1},
        {"url": "https://example.test/a.txt", "blob_ref": blob_ref, "manifest_index": 7, "line": "gamma", "line_index": 2},
    ]
    assert transform.creates_tokens is True
    assert result.success_reason is not None
    assert result.success_reason["action"] == "expanded_blob"
    assert result.success_reason["metadata"]["row_count"] == 3


def test_emitted_rows_satisfy_the_declared_output_schema() -> None:
    """PROVES the emitted shape matches what the transform tells the DAG it emits."""
    transform, result = _run(b"alpha\nbeta\n")

    assert result.rows is not None
    for row in result.rows:
        transform.output_schema.model_validate(row.to_dict(), strict=True)
    assert transform.declared_output_fields == frozenset({"line", "line_index"})


def test_index_field_is_omitted_when_include_index_is_false() -> None:
    """PROVES include_index controls emission rather than being decorative."""
    transform, result = _run(b"alpha\nbeta\n", include_index=False)

    assert result.rows is not None
    assert all("line_index" not in row.to_dict() for row in result.rows)
    assert transform.declared_output_fields == frozenset({"line"})


def test_output_and_index_field_names_are_configurable() -> None:
    """PROVES the emitted column names come from config, not from hardcoded strings."""
    _, result = _run(b"alpha\n", output_field="sentence", index_field="sentence_no")

    assert result.rows is not None
    assert result.rows[0].to_dict()["sentence"] == "alpha"
    assert result.rows[0].to_dict()["sentence_no"] == 0


# --------------------------------------------------------------------------
# Line-boundary semantics.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"alpha\nbeta\n", ["alpha", "beta"]),
        (b"alpha\nbeta", ["alpha", "beta"]),
        (b"alpha\r\nbeta\r\n", ["alpha", "beta"]),
        (b"alpha\rbeta\r", ["alpha", "beta"]),
        (b"alpha\r\nbeta\ngamma\r", ["alpha", "beta", "gamma"]),
    ],
)
def test_recognises_lf_crlf_and_cr_line_endings(body: bytes, expected: list[str]) -> None:
    """PROVES a single trailing terminator ends the last line instead of opening an empty one."""
    _, result = _run(body)

    assert result.status == "success"
    assert result.rows is not None
    assert [row.to_dict()["line"] for row in result.rows] == expected


def test_blank_lines_are_kept_by_default_and_indices_are_source_positions() -> None:
    """PROVES the default is non-lossy: nothing between two newlines is dropped."""
    _, result = _run(b"alpha\n\nbeta\n")

    assert result.rows is not None
    assert [(row.to_dict()["line_index"], row.to_dict()["line"]) for row in result.rows] == [
        (0, "alpha"),
        (1, ""),
        (2, "beta"),
    ]


def test_skip_blank_lines_drops_blanks_but_keeps_true_line_numbers() -> None:
    """PROVES index_field stays the blob position, so skipped lines leave a visible gap."""
    _, result = _run(b"alpha\n\nbeta\n", skip_blank_lines=True)

    assert result.rows is not None
    assert [(row.to_dict()["line_index"], row.to_dict()["line"]) for row in result.rows] == [
        (0, "alpha"),
        (2, "beta"),
    ]


def test_whitespace_only_lines_are_not_blank() -> None:
    """PROVES 'blank' means empty, not 'looks empty' — trimming is not this plugin's job."""
    _, result = _run(b"alpha\n   \nbeta\n", skip_blank_lines=True)

    assert result.rows is not None
    assert [row.to_dict()["line"] for row in result.rows] == ["alpha", "   ", "beta"]


def test_quoting_and_separators_survive_verbatim() -> None:
    """PROVES text is carried, not parsed — the corruption the CSV workaround causes."""
    _, result = _run(b'"hello world"\na,b,c\nhe said "hi"\n')

    assert result.status == "success"
    assert result.rows is not None
    assert [row.to_dict()["line"] for row in result.rows] == ['"hello world"', "a,b,c", 'he said "hi"']


# --------------------------------------------------------------------------
# The explicit-delimiter arm.
# --------------------------------------------------------------------------


def test_delimiter_splits_on_a_literal_separator() -> None:
    """PROVES the delimiter arm splits on the configured string and ignores newlines."""
    _, result = _run(b"alpha;;beta\nwith newline;;gamma", delimiter=";;")

    assert result.status == "success"
    assert result.rows is not None
    assert [row.to_dict()["line"] for row in result.rows] == ["alpha", "beta\nwith newline", "gamma"]


def test_trailing_delimiter_terminates_the_final_chunk() -> None:
    """PROVES the terminator convention applies to both arms, not just newlines."""
    _, result = _run(b"alpha|beta|", delimiter="|")

    assert result.rows is not None
    assert [row.to_dict()["line"] for row in result.rows] == ["alpha", "beta"]


# --------------------------------------------------------------------------
# Zero rows is a fail state. An empty ROW is data; an empty CONTAINER is not.
# --------------------------------------------------------------------------


def test_empty_blob_is_a_value_level_error() -> None:
    """PROVES a blob that yields no rows at all quarantines the input row.

    Zero rows cannot be consumed by anything downstream, so it is a failure to
    produce data rather than an answer. The row leaves through on_error; it
    does not vanish, and no blank row is synthesised to rescue it.
    """
    transform, result = _run(b"")

    assert result.status == "error"
    assert result.retryable is False
    assert result.rows is None
    assert result.row is None
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["error_type"] == "empty_expansion"
    # The zero-emission success path is NOT available to this transform:
    # success_empty() is reserved for filters declaring can_drop_rows.
    assert transform.can_drop_rows is False


def test_blank_lines_are_rows_with_empty_values_not_an_empty_expansion() -> None:
    """PROVES the distinction the fail-state rule turns on.

    A blob of nothing but newlines is NOT an empty container: each pair of
    newlines delimits a line whose value is the empty string, and an empty
    value is data — the text analogue of a CSV row spelled ``,,,``. Three
    rows must be emitted, and the empty-expansion error must NOT fire.
    """
    _, result = _run(b"\n\n\n")

    assert result.status == "success"
    assert result.rows is not None
    assert [row.to_dict()["line"] for row in result.rows] == ["", "", ""]
    assert [row.to_dict()["line_index"] for row in result.rows] == [0, 1, 2]


def test_the_same_blank_blob_is_an_error_once_skip_blank_lines_removes_the_rows() -> None:
    """PROVES the twin of the test above, and that the two are one decision.

    Identical bytes. ``skip_blank_lines: True`` drops every row, leaving an
    empty container — which is the fail state. The pair is what pins the rule:
    emptiness of a VALUE is fine, emptiness of the RESULT is not.
    """
    _, result = _run(b"\n\n\n", skip_blank_lines=True)

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["error_type"] == "empty_expansion"


def test_a_blank_line_among_real_lines_is_kept_as_a_row() -> None:
    """PROVES the empty value survives in the ordinary case too, not just when
    every line is blank — the case an over-eager 'skip empties' fix would break."""
    _, result = _run(b"alpha\n\nbeta\n")

    assert result.status == "success"
    assert result.rows is not None
    assert [row.to_dict()["line"] for row in result.rows] == ["alpha", "", "beta"]


# --------------------------------------------------------------------------
# Bounds.
# --------------------------------------------------------------------------


def test_exceeding_max_output_rows_is_a_value_level_error() -> None:
    """PROVES the ceiling quarantines the row instead of emitting a truncated expansion."""
    _, result = _run(b"a\nb\nc\nd\n", max_output_rows=2)

    assert result.status == "error"
    assert result.retryable is False
    assert result.reason is not None
    assert result.reason["reason"] == "too_many_rows"
    assert result.reason["max_output_rows"] == 2


def test_row_count_reported_is_the_bound_plus_one_because_the_split_stops_there() -> None:
    """PROVES the ceiling is enforced BY the split, not after materialising every chunk.

    An implementation that split the whole blob and then compared lengths would
    report the true count (400 here). Reporting bound+1 is only possible
    because the split itself was told to stop.
    """
    _, result = _run(b"x\n" * 400, max_output_rows=2)

    assert result.reason is not None
    assert result.reason["row_count"] == 3


def test_delimiter_arm_is_bounded_too() -> None:
    """PROVES the ceiling is not newline-only."""
    _, result = _run(b"a|b|c|d", delimiter="|", max_output_rows=2)

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "too_many_rows"


def test_split_bounded_stops_at_the_ceiling_in_both_arms() -> None:
    """PROVES the split itself is capped: 400 separators yield max_chunks+1 chunks, not 400."""
    newline_chunks = _split_bounded("x\n" * 400, delimiter=None, max_chunks=2)
    assert len(newline_chunks) == 3
    assert newline_chunks[:2] == ["x", "x"]
    assert newline_chunks[2].count("\n") == 397

    delimiter_chunks = _split_bounded("a|" * 400, delimiter="|", max_chunks=2)
    assert len(delimiter_chunks) == 3
    assert delimiter_chunks[:2] == ["a", "a"]
    assert delimiter_chunks[2].count("|") == 397


def test_split_bounded_is_off_by_one_correct() -> None:
    """PROVES exactly max_chunks is accepted and one more is over."""
    assert _split_bounded("a\nb\n", delimiter=None, max_chunks=2) == ["a", "b"]
    assert len(_split_bounded("a\nb\nc\n", delimiter=None, max_chunks=2)) == 3
    assert _split_bounded("a|b", delimiter="|", max_chunks=2) == ["a", "b"]
    assert len(_split_bounded("a|b|c", delimiter="|", max_chunks=2)) == 3


def test_blob_over_max_blob_bytes_is_a_value_level_error() -> None:
    """PROVES the byte ceiling is checked before decoding."""
    body = b"a\nb\nc\n"
    _, result = _run(body, max_blob_bytes=3)

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "blob_too_large"
    assert result.reason["body_size"] == len(body)
    assert result.reason["max_blob_bytes"] == 3


# --------------------------------------------------------------------------
# Decoding.
# --------------------------------------------------------------------------


def test_undecodable_bytes_are_quarantined_with_no_replacement_character() -> None:
    """PROVES strict decoding: no U+FFFD ever reaches a row under a success status."""
    _, result = _run(b"alpha\n\xff\xfe\nbeta\n")

    assert result.status == "error"
    assert result.rows is None
    assert result.row is None
    assert result.reason is not None
    assert result.reason["reason"] == "decode_failed"
    assert result.reason["encoding"] == "utf-8"
    assert "�" not in repr(result.reason)


def test_encoding_option_selects_the_codec() -> None:
    """CONTROL for the test above: the same bytes decode cleanly under latin-1."""
    _, result = _run("café\nnaïve\n".encode("latin-1"), encoding="latin-1")

    assert result.status == "success"
    assert result.rows is not None
    assert [row.to_dict()["line"] for row in result.rows] == ["café", "naïve"]


def test_utf8_bom_is_carried_verbatim_into_the_first_row() -> None:
    """PROVES the documented BOM behaviour; utf-8-sig is the operator's remedy."""
    body = "﻿alpha\nbeta\n".encode()
    _, result = _run(body)

    assert result.rows is not None
    assert result.rows[0].to_dict()["line"] == "﻿alpha"

    _, sig_result = _run(body, encoding="utf-8-sig")
    assert sig_result.rows is not None
    assert sig_result.rows[0].to_dict()["line"] == "alpha"


# --------------------------------------------------------------------------
# Payload-store failures.
# --------------------------------------------------------------------------


def test_missing_blob_is_a_value_level_error() -> None:
    """PROVES a purged payload quarantines the row instead of crashing the run."""
    transform = _build_transform()
    transform._payload_store = _PayloadStoreFake({})
    absent = _hash(b"never stored")

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.txt", "blob_ref": absent}),
        make_context(),
    )

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "blob_not_found"
    assert result.reason["blob_ref"] == absent


class _CorruptAndMissingError(IntegrityError, PayloadNotFoundError):
    """A store error that is BOTH kinds at once.

    The discriminating case for handler ORDER. ``IntegrityError`` and
    ``PayloadNotFoundError`` are unrelated siblings today (both derive
    straight from ``Exception``), so a single-kind integrity failure
    propagates no matter which clause is written first — or whether the
    integrity clause exists at all. Only an error matching both clauses can
    tell the orderings apart, because ``except`` matches in source order.
    """

    def __init__(self, content_hash: str) -> None:
        PayloadNotFoundError.__init__(self, content_hash)


class _CorruptAndMissingStore:
    def retrieve(self, content_hash: str) -> bytes:
        raise _CorruptAndMissingError(content_hash)


def test_integrity_failure_propagates_even_when_the_error_is_also_a_not_found() -> None:
    """PROVES the integrity clause is ordered ABOVE blob_not_found.

    A corrupt payload discovered while resolving a reference must crash the
    run, never be downgraded to a routed value-level error that lets the row
    continue quietly down the on_error path. Reverse the two clauses and this
    test goes red while every other test in the file stays green — which is
    the whole reason it is written with a dual-kind error rather than a plain
    ``IntegrityError``.
    """
    transform = _build_transform()
    transform._payload_store = _CorruptAndMissingStore()

    with pytest.raises(IntegrityError):
        transform.process(
            make_pipeline_row({"url": "https://example.test/a.txt", "blob_ref": _hash(b"x")}),
            make_context(),
        )


def test_integrity_ordering_still_routes_a_genuinely_missing_blob() -> None:
    """CONTROL for the test above: quarantine of an ordinary missing blob survives.

    Without this, re-raising EVERYTHING from the retrieve seam would satisfy
    the integrity test while destroying the routing path that a purged or
    stale payload depends on.
    """
    transform = _build_transform()
    transform._payload_store = _PayloadStoreFake({})
    absent = _hash(b"never stored")

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.txt", "blob_ref": absent}),
        make_context(),
    )

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "blob_not_found"


def test_integrity_failure_propagates_rather_than_being_quarantined() -> None:
    """A plain IntegrityError propagates. Kept as the ordinary-path case, but
    note it does NOT discriminate handler order — the dual-kind test above is
    what pins that."""
    transform = _build_transform()
    transform._payload_store = _IntegrityFailingStore()

    with pytest.raises(IntegrityError):
        transform.process(
            make_pipeline_row({"url": "https://example.test/a.txt", "blob_ref": _hash(b"x")}),
            make_context(),
        )


def test_malformed_blob_ref_is_a_value_level_error(tmp_path: Path) -> None:
    """PROVES a non-hash reference never reaches the payload store."""
    transform = _build_transform()
    transform._payload_store = FilesystemPayloadStore(tmp_path / "payloads")

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.txt", "blob_ref": "not-a-sha256"}),
        make_context(),
    )

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["error_type"] == "invalid_blob_ref"


def test_uppercase_hex_blob_ref_is_rejected() -> None:
    """PROVES the hash check is exact, not merely a length check."""
    transform = _build_transform()
    transform._payload_store = _PayloadStoreFake({})

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.txt", "blob_ref": "A" * 64}),
        make_context(),
    )

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"


def test_non_string_blob_ref_crashes_as_an_upstream_bug() -> None:
    """PROVES a type violation is a framework bug, not routed data."""
    transform = _build_transform()
    transform._payload_store = _PayloadStoreFake({})

    with pytest.raises(TypeError, match="must be a string payload-store hash"):
        transform.process(
            make_pipeline_row({"url": "https://example.test/a.txt", "blob_ref": 12345}),
            make_context(),
        )


# --------------------------------------------------------------------------
# Collisions with arriving columns.
# --------------------------------------------------------------------------


def test_emitted_field_colliding_with_an_input_field_is_a_value_level_error() -> None:
    """PROVES an arriving column is never silently overwritten by an emitted one."""
    _, result = _run(b"alpha\n", row={"line": "already here"})

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "field_collision"
    assert result.reason["fields"] == ["line"]


def test_index_field_collision_is_reported_alongside_the_output_field() -> None:
    """PROVES both emitted names are checked, not just the first."""
    _, result = _run(b"alpha\n", row={"line": "x", "line_index": 99})

    assert result.reason is not None
    assert result.reason["fields"] == ["line", "line_index"]


def test_empty_blob_reports_the_empty_expansion_not_a_phantom_collision() -> None:
    """PROVES the collision guard reports overwrites, not merely matching names.

    Nothing was emitted, so nothing was overwritten. The row must quarantine
    for the reason that is true — the expansion produced no rows — rather than
    for a field conflict that never occurred.
    """
    _, result = _run(b"", row={"line": "already here"})

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["error_type"] == "empty_expansion"


def test_index_field_collision_is_not_reported_when_the_index_is_off() -> None:
    """CONTROL: the collision above is caused by emitting the field, not by its name."""
    _, result = _run(b"alpha\n", row={"line_index": 99}, include_index=False)

    assert result.status == "success"
    assert result.rows is not None
    assert result.rows[0].to_dict()["line_index"] == 99


# --------------------------------------------------------------------------
# Configuration rejections.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected_fragment"),
    [
        ({"blob_ref_field": "line"}, "blob_ref_field"),
        ({"blob_ref_field": "line_index"}, "blob_ref_field"),
        ({"output_field": "line_index"}, "index_field"),
        ({"output_field": "   "}, "output_field"),
        ({"output_field": "not an identifier"}, "output_field"),
        ({"index_field": "9lives"}, "index_field"),
        ({"blob_ref_field": "  "}, "blob_ref_field"),
        ({"delimiter": ""}, "delimiter"),
        ({"encoding": "definitely-not-a-codec"}, "encoding"),
        ({"max_output_rows": 0}, "max_output_rows"),
        ({"max_blob_bytes": 0}, "max_blob_bytes"),
    ],
)
def test_config_rejections_name_the_offending_option(overrides: dict[str, Any], expected_fragment: str) -> None:
    """PROVES every rejection is attributable to a named option the author can fix."""
    with pytest.raises(PluginConfigError) as exc_info:
        _build_transform(**overrides)

    assert expected_fragment in str(exc_info.value)


def test_repointing_blob_ref_field_at_an_ordinary_column_is_accepted() -> None:
    """CONTROL for the rejections above: only CREATED names are refused."""
    transform = _build_transform(blob_ref_field="document_hash")

    assert transform._blob_ref_field == "document_hash"


def test_index_field_may_equal_blob_ref_field_when_the_index_is_off() -> None:
    """PROVES the collision guard tracks what is emitted, not what is merely configured."""
    transform = _build_transform(index_field="blob_ref", include_index=False)

    assert transform.declared_output_fields == frozenset({"line"})


# --------------------------------------------------------------------------
# Declarations and registration.
# --------------------------------------------------------------------------


def test_determinism_is_declared_on_the_class_itself() -> None:
    """PROVES the IO_READ classification is this plugin's, not inherited."""
    assert BlobTextExpand.__dict__["determinism"] is Determinism.IO_READ


def test_discovery_registers_blob_text_expand() -> None:
    from elspeth.plugins.infrastructure.manager import PluginManager

    manager = PluginManager()
    manager.register_builtin_plugins()

    transform = manager.get_transform_by_name("blob_text_expand")
    assert transform.name == "blob_text_expand"


# --------------------------------------------------------------------------
# An input locator may not name a column this transform creates.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("created", ["line", "line_index"])
def test_blob_ref_field_may_not_name_a_created_column(created: str) -> None:
    """PROVES both validation paths refuse a locator aimed at an emitted column.

    Pointing the input locator at a created field leaves that field consumed,
    so it is never demoted, so every row is rejected for missing the field the
    transform exists to produce (elspeth-d6eeb3a71d).
    """
    from elspeth.plugins.infrastructure.validation import validate_transform_config

    config = {"schema": DYNAMIC_SCHEMA, "blob_ref_field": created}

    errors = [error.message for error in validate_transform_config("blob_text_expand", config)]
    assert errors, "pre-validation accepted a locator naming a created column"
    assert any("blob_ref_field" in message or created in message for message in errors)

    with pytest.raises(PluginConfigError):
        BlobTextExpand(config)


def test_created_field_guard_is_live_not_merely_declared() -> None:
    """PROVES the _reject_input_options_naming_created_fields call actually fires.

    The config validator refuses these collisions by name and wins today, which
    would let a dead guard call pass every rejection test above. This drives the
    guard directly against the live created set, which is the surface that keeps
    covering a created field the by-name validator cannot see.
    """
    transform = _build_transform()

    with pytest.raises(PluginConfigError, match="which blob_text_expand itself creates"):
        transform._reject_input_options_naming_created_fields({"blob_ref_field": "line"})

    # CONTROL: an arriving column passes, so the raise above is caused by the
    # column being CREATED rather than by the guard rejecting everything.
    transform._reject_input_options_naming_created_fields({"blob_ref_field": "url"})
    transform._reject_input_options_naming_created_fields({"blob_ref_field": None})


def test_init_wires_the_created_field_guard_to_the_LIVE_created_set() -> None:
    """PROVES the guard is called from __init__, not merely defined.

    The by-name config validator refuses every collision it can enumerate and
    wins today, so removing the __init__ call changes no rejection this suite
    otherwise makes — the call would be silently deletable. This patches the
    created set to hold a name the validator never inspects, which is exactly
    the future the guard exists for: a created field that is not knowable from
    the option names alone. Construction must still be refused.
    """
    from unittest.mock import PropertyMock, patch

    config = {"schema": DYNAMIC_SCHEMA, "blob_ref_field": "url"}  # an ARRIVING column: the validator accepts it
    assert BlobTextExpand(config), "control: the config must be acceptable before the created set is patched"

    with patch.object(BlobTextExpand, "self_created_input_fields", new_callable=PropertyMock) as created:
        created.return_value = frozenset({"url"})
        with pytest.raises(PluginConfigError, match="itself creates"):
            BlobTextExpand(config)
