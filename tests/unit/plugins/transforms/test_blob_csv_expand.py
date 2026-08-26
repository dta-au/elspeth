"""Tests for blob_csv_expand transform."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.payload_store import IntegrityError, PayloadNotFoundError
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.blob_csv_expand import BlobCSVExpand, BlobCSVExpandConfig
from elspeth.plugins.transforms.blob_expand_contract import DEFAULT_BLOB_REF_FIELD, DEFAULT_TEXT_FIELD
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
        return self.content_by_hash[content_hash]

    def exists(self, content_hash: str) -> bool:
        return content_hash in self.content_by_hash

    def delete(self, content_hash: str) -> bool:
        return self.content_by_hash.pop(content_hash, None) is not None


class _FakeLifecycleContext:
    """Minimal LifecycleContext double — mirrors test_pdf_rasterize.py's."""

    def __init__(self, payload_store: Any) -> None:
        self.payload_store = payload_store


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _build_transform(**overrides: Any) -> BlobCSVExpand:
    config = {
        "schema": DYNAMIC_SCHEMA,
        "blob_ref_field": "blob_ref",
    }
    config.update(overrides)
    return BlobCSVExpand(config)


def test_blob_csv_expand_parses_rows_and_preserves_url_order() -> None:
    body = b"id,name\n1,alice\n2,bob\n"
    blob_ref = _hash(body)
    transform = _build_transform()
    transform._payload_store = _PayloadStoreFake({blob_ref: body})

    result = transform.process(
        make_pipeline_row(
            {
                "url": "https://example.test/a.csv",
                "blob_ref": blob_ref,
                "manifest_index": 0,
            }
        ),
        make_context(),
    )

    assert result.status == "success"
    assert result.is_multi_row
    assert result.rows is not None
    assert [row.to_dict() for row in result.rows] == [
        {
            "url": "https://example.test/a.csv",
            "blob_ref": blob_ref,
            "manifest_index": 0,
            "id": "1",
            "name": "alice",
            "csv_row_index": 0,
        },
        {
            "url": "https://example.test/a.csv",
            "blob_ref": blob_ref,
            "manifest_index": 0,
            "id": "2",
            "name": "bob",
            "csv_row_index": 1,
        },
    ]
    assert transform.creates_tokens is True


def test_blob_csv_expand_supports_headerless_columns() -> None:
    body = b"1,alice\n2,bob\n"
    blob_ref = _hash(body)
    transform = _build_transform(columns=["id", "name"])
    transform._payload_store = _PayloadStoreFake({blob_ref: body})

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "blob_ref": blob_ref}),
        make_context(),
    )

    assert result.status == "success"
    assert result.rows is not None
    assert [row.to_dict()["name"] for row in result.rows] == ["alice", "bob"]


def test_blob_csv_expand_fails_on_field_collision() -> None:
    body = b"url,name\nhttps://other.test,alice\n"
    blob_ref = _hash(body)
    transform = _build_transform()
    transform._payload_store = _PayloadStoreFake({blob_ref: body})

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "blob_ref": blob_ref}),
        make_context(),
    )

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "field_collision"
    assert result.reason["fields"] == ["url"]


def test_blob_csv_expand_enforces_max_output_rows() -> None:
    body = b"id\n1\n2\n"
    blob_ref = _hash(body)
    transform = _build_transform(max_output_rows=1)
    transform._payload_store = _PayloadStoreFake({blob_ref: body})

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "blob_ref": blob_ref}),
        make_context(),
    )

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "too_many_rows"
    assert result.reason["max_output_rows"] == 1


def test_blob_csv_expand_routes_malformed_manifest_blob_ref_as_row_error(tmp_path: Path) -> None:
    transform = _build_transform()
    transform._payload_store = FilesystemPayloadStore(tmp_path / "payloads")

    result = transform.process(
        make_pipeline_row(
            {
                "url": "https://example.test/manifest-row.json",
                "blob_ref": "not-a-sha256",
                "manifest_index": 0,
            }
        ),
        make_context(),
    )

    assert result.status == "error"
    assert result.retryable is False
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["error_type"] == "invalid_blob_ref"
    assert result.reason["field"] == "blob_ref"
    assert result.reason["blob_ref"] == "not-a-sha256"


def test_discovery_registers_blob_csv_expand() -> None:
    from elspeth.plugins.infrastructure.manager import PluginManager

    manager = PluginManager()
    manager.register_builtin_plugins()

    transform = manager.get_transform_by_name("blob_csv_expand")
    assert transform.name == "blob_csv_expand"


# ── source: field (the inline arm) ────────────────────────────────────────────
#
# The arm reads CSV TEXT straight off the row instead of retrieving bytes from
# the payload store. Everything after the read is the same parser, and these
# tests are written to prove that rather than to restate it.

FIELD_SCHEMA = {"mode": "observed", "guaranteed_fields": ["url", "content"]}

CSV_TEXT = "id,name\n1,alice\n2,bob\n"

# A short record whose second data row carries one value where the header
# promised two — the same defect must be reported identically by both arms.
MALFORMED_CSV_TEXT = "id,name\n1,alice\n2\n"

# The column each arm reads its input from. Dropping both is what lets two rows
# built from different arms be compared for equality at all.
_ARM_LOCATOR_FIELDS = frozenset({"blob_ref", "content"})


def _build_field_transform(**overrides: Any) -> BlobCSVExpand:
    config: dict[str, Any] = {
        "schema": FIELD_SCHEMA,
        "source": "field",
        "text_field": "content",
    }
    config.update(overrides)
    return BlobCSVExpand(config)


def _parsed_rows(result: Any) -> list[dict[str, Any]]:
    """Emitted rows minus whichever locator column the arm read from."""
    assert result.rows is not None
    return [{key: value for key, value in row.to_dict().items() if key not in _ARM_LOCATOR_FIELDS} for row in result.rows]


def test_blob_csv_expand_field_arm_parses_inline_csv_text() -> None:
    transform = _build_field_transform()

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "content": CSV_TEXT}),
        make_context(),
    )

    assert result.status == "success"
    assert result.is_multi_row
    assert result.rows is not None
    # `content` is absent by design: the inline arm CONSUMES its text column
    # rather than copying the whole source document onto every emitted row.
    # See test_blob_csv_expand_field_arm_drops_the_consumed_text_column.
    assert [row.to_dict() for row in result.rows] == [
        {
            "url": "https://example.test/a.csv",
            "id": "1",
            "name": "alice",
            "csv_row_index": 0,
        },
        {
            "url": "https://example.test/a.csv",
            "id": "2",
            "name": "bob",
            "csv_row_index": 1,
        },
    ]
    assert result.reason is None


def test_blob_csv_expand_both_arms_emit_identical_rows_from_identical_csv() -> None:
    """The strongest available proof that the two arms share one parser."""
    body = CSV_TEXT.encode("utf-8")
    blob_ref = _hash(body)
    blob_transform = _build_transform()
    blob_transform._payload_store = _PayloadStoreFake({blob_ref: body})
    field_transform = _build_field_transform()

    blob_result = blob_transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "blob_ref": blob_ref}),
        make_context(),
    )
    field_result = field_transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "content": CSV_TEXT}),
        make_context(),
    )

    assert blob_result.status == "success"
    assert field_result.status == "success"
    assert _parsed_rows(blob_result) == _parsed_rows(field_result)
    # Pinned literally as well, so a shared regression in BOTH arms cannot pass
    # by making two wrong answers agree with each other.
    assert _parsed_rows(field_result) == [
        {"url": "https://example.test/a.csv", "id": "1", "name": "alice", "csv_row_index": 0},
        {"url": "https://example.test/a.csv", "id": "2", "name": "bob", "csv_row_index": 1},
    ]


def test_blob_csv_expand_both_arms_report_a_parse_defect_identically() -> None:
    """The error taxonomy below the read is shared, not forked per arm."""
    body = MALFORMED_CSV_TEXT.encode("utf-8")
    blob_ref = _hash(body)
    blob_transform = _build_transform()
    blob_transform._payload_store = _PayloadStoreFake({blob_ref: body})
    field_transform = _build_field_transform()

    blob_result = blob_transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "blob_ref": blob_ref}),
        make_context(),
    )
    field_result = field_transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "content": MALFORMED_CSV_TEXT}),
        make_context(),
    )

    assert blob_result.status == "error"
    assert field_result.status == "error"
    # A parse defect is a property of the CSV, not of where the CSV came from,
    # so these reasons carry no arm-specific identity and must be EQUAL.
    assert blob_result.reason == field_result.reason
    assert blob_result.reason is not None
    assert blob_result.reason["reason"] == "csv_column_count_mismatch"
    assert blob_result.reason["expected"] == 2
    assert blob_result.reason["actual"] == 1
    assert blob_result.reason["row_number"] == 2


def test_blob_csv_expand_field_arm_collides_with_an_existing_input_field() -> None:
    transform = _build_field_transform()

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "content": "url,name\nhttps://other.test,alice\n"}),
        make_context(),
    )

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "field_collision"
    assert result.reason["fields"] == ["url"]


def test_blob_csv_expand_field_arm_routes_a_missing_text_field_as_a_row_error() -> None:
    transform = _build_field_transform()

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "other": "x"}),
        make_context(),
    )

    assert result.status == "error"
    assert result.retryable is False
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["error_type"] == "missing_text_field"
    assert result.reason["field"] == "content"


def test_blob_csv_expand_field_arm_routes_an_empty_text_field_as_a_row_error() -> None:
    transform = _build_field_transform()

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "content": "   \n"}),
        make_context(),
    )

    assert result.status == "error"
    assert result.retryable is False
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["error_type"] == "empty_text_field"
    assert result.reason["field"] == "content"


def test_blob_csv_expand_field_arm_routes_a_non_string_text_field_as_a_row_error() -> None:
    """Row values are upstream data, so a wrong type is quarantined, not raised."""
    transform = _build_field_transform()

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "content": 17}),
        make_context(),
    )

    assert result.status == "error"
    assert result.retryable is False
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["error_type"] == "invalid_text_field"
    assert result.reason["field"] == "content"


def test_blob_csv_expand_field_arm_declares_the_text_field_as_its_input() -> None:
    field_transform = _build_field_transform()
    blob_transform = _build_transform()

    assert "content" in field_transform.declared_input_fields
    assert "blob_ref" not in field_transform.declared_input_fields
    assert "blob_ref" in blob_transform.declared_input_fields


def test_blob_csv_expand_field_arm_needs_no_payload_store() -> None:
    """A node that never touches the store must not be blocked on having one."""
    transform = _build_field_transform()

    transform.on_start(_FakeLifecycleContext(None))

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "content": CSV_TEXT}),
        make_context(),
    )
    assert result.status == "success"


def test_blob_csv_expand_blob_arm_still_requires_a_payload_store() -> None:
    transform = _build_transform()

    with pytest.raises(FrameworkBugError):
        transform.on_start(_FakeLifecycleContext(None))


def test_blob_csv_expand_field_arm_probe_row_carries_csv_text_not_a_blob_ref() -> None:
    """The invariant probe must feed the arm the plugin is actually configured for."""
    transform = _build_field_transform()

    probe_rows = transform.forward_invariant_probe_rows(make_pipeline_row({"url": "https://example.test/a.csv"}))

    assert len(probe_rows) == 1
    probed = probe_rows[0].to_dict()
    assert "blob_ref" not in probed
    assert probed["content"] == "blob_csv_expand_probe_value\nprobe\n"
    assert transform.process(probe_rows[0], make_context()).status == "success"


# ── configuration ────────────────────────────────────────────────────────────


def test_blob_csv_expand_field_arm_rejects_row_index_field_colliding_with_text_field() -> None:
    with pytest.raises(PluginConfigError, match="text_field"):
        _build_field_transform(text_field="content", row_index_field="content")


def test_blob_csv_expand_field_arm_rejects_text_field_declared_in_columns() -> None:
    with pytest.raises(PluginConfigError, match="text_field"):
        _build_field_transform(columns=["content", "name"])


def test_blob_csv_expand_blob_arm_rejects_text_field_naming_a_created_field() -> None:
    """The created-field sweep mutates EVERY ``*_field`` option, this one included."""
    with pytest.raises(PluginConfigError, match="text_field"):
        _build_transform(text_field="csv_row_index")


def test_blob_csv_expand_blob_arm_accepts_text_field_repointed_at_an_ordinary_column() -> None:
    """The sweep's control step: the option must be repointable before it refuses."""
    transform = _build_transform(text_field="arriving_column_under_test")

    assert transform.declared_input_fields >= {"blob_ref"}


def test_blob_csv_expand_blob_arm_still_accepts_a_row_index_named_like_the_text_default() -> None:
    """A shipped config that never mentions ``source`` cannot start failing.

    Left unset, ``text_field`` names no column, so emitting the row index into a
    column called ``content`` was legal before this option existed and stays legal.
    """
    body = b"id,name\n1,alice\n"
    blob_ref = _hash(body)
    transform = _build_transform(row_index_field="content")
    transform._payload_store = _PayloadStoreFake({blob_ref: body})

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "blob_ref": blob_ref}),
        make_context(),
    )

    assert result.status == "success"
    assert result.rows is not None
    assert result.rows[0].to_dict()["content"] == 0


def test_blob_csv_expand_default_config_is_the_blob_arm_with_the_shared_default() -> None:
    """A config that does not mention ``source`` behaves exactly as it did before."""
    config = BlobCSVExpandConfig.from_dict({"schema": DYNAMIC_SCHEMA}, plugin_name="blob_csv_expand")

    assert config.source == "blob"
    # blob_ref_field now defaults to None for the same reason text_field does —
    # a defaulted spelling leaks onto the arm that never reads it. The
    # behavioural claim this test makes is unchanged and is asserted below on
    # the surface that now carries it: the blob arm still reads and declares
    # the shared default spelling.
    assert config.blob_ref_field is None
    assert config.read_blob_ref_field == DEFAULT_BLOB_REF_FIELD == "blob_ref"
    assert config.named_blob_ref_field == "blob_ref"
    assert config.text_field is None
    assert config.declared_input_fields >= {"blob_ref"}
    assert "content" not in config.declared_input_fields


def test_blob_csv_expand_blob_arm_does_not_acquire_a_text_column_requirement() -> None:
    """The inline arm's option must not un-demote a CSV column on the blob arm.

    ``consumed_input_fields`` folds in the value of every ``*_field`` option, and
    ``input_schema`` demotes only created fields that are NOT consumed. If
    ``text_field`` defaulted to the string ``content`` rather than to None, a blob
    config whose CSV carries a ``content`` column would stop demoting it and reject
    every row for missing the field the transform exists to create — from a config
    that never mentions ``source``. Measured against the pre-change plugin.
    """
    transform = _build_transform(
        schema={
            "mode": "fixed",
            "fields": [
                {"name": "blob_ref", "field_type": "str", "required": True},
                {"name": "content", "field_type": "str", "required": True},
            ],
        },
        columns=["id", "content"],
    )

    assert "content" not in transform.consumed_input_fields
    assert "content" in transform.demoted_input_fields
    assert not transform.input_schema.model_fields["content"].is_required()
    assert "content" not in _build_transform().consumed_input_fields


def test_blob_csv_expand_field_arm_defaults_the_text_field_to_the_shared_spelling() -> None:
    """``source: field`` with no ``text_field`` reads the family-wide default column."""
    transform = _build_field_transform(text_field=None)

    assert transform.declared_input_fields >= {DEFAULT_TEXT_FIELD}
    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", DEFAULT_TEXT_FIELD: CSV_TEXT}),
        make_context(),
    )
    assert result.status == "success"
    assert _parsed_rows(result) == [
        {"url": "https://example.test/a.csv", "id": "1", "name": "alice", "csv_row_index": 0},
        {"url": "https://example.test/a.csv", "id": "2", "name": "bob", "csv_row_index": 1},
    ]


def test_blob_csv_expand_field_arm_rejects_a_row_index_colliding_with_the_defaulted_text_field() -> None:
    """The collision guard resolves the default, so leaving ``text_field`` unset is not an escape."""
    with pytest.raises(PluginConfigError, match="text_field"):
        _build_field_transform(text_field=None, row_index_field=DEFAULT_TEXT_FIELD)


def test_blob_csv_expand_blob_arm_empty_csv_error_is_exactly_as_shipped() -> None:
    """A CSV with no data rows is an ERROR that quarantines the row, not a zero-row success.

    Pinned as a whole dict because this is shipped behaviour other configs
    depend on: same literal, same keys, same message.
    """
    body = b"id,name\n"
    blob_ref = _hash(body)
    transform = _build_transform()
    transform._payload_store = _PayloadStoreFake({blob_ref: body})

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "blob_ref": blob_ref}),
        make_context(),
    )

    assert result.status == "error"
    assert result.retryable is False
    assert result.rows is None
    assert result.reason == {
        "reason": "empty_csv",
        "field": "blob_ref",
        "blob_ref": blob_ref,
        "error": "CSV blob had no data rows",
    }


def test_blob_csv_expand_field_arm_header_only_text_is_an_empty_csv_row_error() -> None:
    """The inline arm reaches the SAME empty-CSV disposition as the shipped blob arm.

    The two reasons differ only in the identity of what was read — the field
    arm has no ``blob_ref`` to name, and saying "blob" of a row field would be
    a lie. The literal, the error-vs-success disposition, and the quarantine
    are identical, and the blob arm's message is untouched.
    """
    transform = _build_field_transform()

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "content": "id,name\n"}),
        make_context(),
    )

    assert result.status == "error"
    assert result.retryable is False
    assert result.rows is None
    assert result.reason == {
        "reason": "empty_csv",
        "field": "content",
        "error": "CSV text had no data rows",
    }


@pytest.mark.parametrize(
    ("label", "options", "text"),
    [
        ("header with no data rows", {}, "id,name\n"),
        ("skip_rows consumes the document", {"skip_rows": 3}, "id,name\n1,alice\n"),
        ("whitespace only", {}, "  \n \n"),
        ("declared columns, no records", {"columns": ["id", "name"]}, "\n\n"),
    ],
)
def test_blob_csv_expand_field_arm_never_succeeds_with_zero_rows(label: str, options: dict[str, Any], text: str) -> None:
    """There is no zero-row success path on the inline arm.

    Whatever the reason literal, input that yields no rows must fail so the row
    quarantines through ``on_error``. A success carrying no rows would strand
    the input silently.
    """
    transform = _build_field_transform(**options)

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "content": text}),
        make_context(),
    )

    assert result.status == "error", label
    assert result.rows is None, label
    assert result.reason is not None


# --------------------------------------------------------------------------
# An input locator may not name a column this transform CREATES, and the
# inline arm may not acquire a phantom blob_ref requirement it never reads.
# --------------------------------------------------------------------------


def _prevalidation_errors(config: dict[str, Any]) -> list[str]:
    """Errors from the pre-validation path, which runs the config model alone."""
    from elspeth.plugins.infrastructure.validation import validate_transform_config

    return [error.message for error in validate_transform_config("blob_csv_expand", config)]


@pytest.mark.parametrize(
    ("overrides", "expected_fragment"),
    [
        ({"columns": ["a", "b"], "blob_ref_field": "a"}, "blob_ref_field"),
        ({"source": "field", "text_field": "content", "columns": ["a", "b"], "blob_ref_field": "a"}, "blob_ref_field"),
        ({"blob_ref_field": "csv_row_index"}, "blob_ref_field"),
        ({"source": "field", "text_field": "a", "columns": ["a", "b"]}, "text_field"),
    ],
)
def test_input_locator_may_not_name_a_created_column(overrides: dict[str, Any], expected_fragment: str) -> None:
    """PROVES a locator pointed at a created column is refused on BOTH arms.

    ``columns`` is the half the by-name config comparisons cannot cover: only
    the live created set knows its members. Naming one leaves that column
    consumed, so it is never demoted, so every row is rejected for missing the
    field this transform exists to produce (elspeth-d6eeb3a71d).
    """
    config: dict[str, Any] = {"schema": DYNAMIC_SCHEMA}
    config.update(overrides)

    errors = _prevalidation_errors(config)
    assert errors, "pre-validation accepted a locator naming a created column"
    assert any(expected_fragment in message for message in errors)

    with pytest.raises(Exception) as exc_info:
        BlobCSVExpand(config)
    assert expected_fragment in str(exc_info.value)


def test_prevalidation_and_engine_agree_on_an_acceptable_locator() -> None:
    """CONTROL: a locator pointed at an arriving column is accepted by both paths."""
    config = {"schema": DYNAMIC_SCHEMA, "columns": ["a", "b"], "blob_ref_field": "document_hash"}

    assert _prevalidation_errors(config) == []
    assert BlobCSVExpand(config)._blob_ref_field == "document_hash"


def test_inline_arm_does_not_consume_the_blob_reference_column() -> None:
    """PROVES the arm that never reads a payload hash does not require one.

    ``_config_named_input_columns`` is arm-unaware, so a defaulted spelling
    reached ``consumed_input_fields`` on the inline arm too. The option now
    defaults to None and ``read_blob_ref_field`` restores the effective value.
    """
    transform = BlobCSVExpand({"schema": {"mode": "observed"}, "source": "field", "text_field": "content", "columns": ["a", "b"]})

    assert "blob_ref" not in transform.consumed_input_fields
    assert sorted(transform.consumed_input_fields) == ["content"]


def test_blob_arm_still_consumes_the_default_blob_reference_column() -> None:
    """CONTROL, and the backward-compatibility contract: omitting the option on
    the blob arm must still declare and consume ``blob_ref``. This is the pin
    that a None default must not be allowed to quietly break."""
    transform = BlobCSVExpand({"schema": {"mode": "observed"}, "columns": ["a", "b"]})

    assert "blob_ref" in transform.consumed_input_fields
    assert transform._blob_ref_field == "blob_ref"


def test_inline_arm_may_expand_a_csv_column_named_blob_ref() -> None:
    """PROVES the silent case is closed: an author who selects the inline arm,
    never writes blob_ref_field, and has a CSV column called ``blob_ref`` gets a
    working transform rather than every row rejected.

    Asserted under ``mode: fixed``, because demotion only has a target when the
    schema actually declares the field — under ``observed`` there is nothing to
    demote and the assertion would pass vacuously.
    """
    fixed = {
        "mode": "fixed",
        "fields": [
            {"name": "content", "type": "str", "required": True, "nullable": False},
            {"name": "blob_ref", "type": "str", "required": True, "nullable": False},
            {"name": "b", "type": "str", "required": True, "nullable": False},
        ],
    }
    transform = BlobCSVExpand({"schema": fixed, "source": "field", "text_field": "content", "columns": ["blob_ref", "b"]})

    assert "blob_ref" in transform.self_created_input_fields
    assert "blob_ref" not in transform.consumed_input_fields
    assert "blob_ref" in transform.demoted_input_fields
    # The created column is therefore NOT required on input — the row-rejecting
    # outcome this whole fix exists to prevent.
    assert transform.input_schema.model_fields["blob_ref"].is_required() is False
    assert transform.input_schema.model_fields["content"].is_required() is True


def test_created_field_guard_is_live_not_merely_declared() -> None:
    """PROVES the _reject_input_options_naming_created_fields call actually fires.

    The config validator refuses every collision it can name, and wins today,
    which would let a dead guard call pass every rejection test above. This
    drives the guard against the live created set — the surface that keeps
    covering a created field the by-name validator cannot see.
    """
    transform = _build_transform(columns=["a", "b"])

    with pytest.raises(PluginConfigError, match="which blob_csv_expand itself creates"):
        transform._reject_input_options_naming_created_fields({"blob_ref_field": "a"})

    # CONTROL: an arriving column passes, so the raise above is attributable to
    # the column being CREATED rather than to the guard rejecting everything.
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
    assert BlobCSVExpand(config), "control: the config must be acceptable before the created set is patched"

    with patch.object(BlobCSVExpand, "self_created_input_fields", new_callable=PropertyMock) as created:
        created.return_value = frozenset({"url"})
        with pytest.raises(PluginConfigError, match="itself creates"):
            BlobCSVExpand(config)


# ── empty ROW vs empty FILE (John's ruling, 2026-08-26) ──────────────────────


def test_blob_csv_expand_emits_a_row_for_a_record_whose_values_are_all_empty() -> None:
    """An empty ROW is data; only an empty FILE is the failure.

    ``id,name,note\\n,,\\n`` is a header plus one record whose three values are
    empty strings. That record MUST emit one successful row carrying those empty
    values — it is a real row that happens to hold no data. Contrast
    ``test_blob_csv_expand_blob_arm_empty_csv_error_is_exactly_as_shipped``,
    where the document has no data rows at all and is an ``empty_csv`` error.
    This is the case most at risk of being "fixed" into an error by someone
    applying the empty-file rule too broadly.
    """
    body = b"id,name,note\n,,\n"
    blob_ref = _hash(body)
    transform = _build_transform()
    transform._payload_store = _PayloadStoreFake({blob_ref: body})

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "blob_ref": blob_ref}),
        make_context(),
    )

    assert result.status == "success"
    assert result.rows is not None
    assert len(result.rows) == 1
    emitted = result.rows[0].to_dict()
    assert emitted["id"] == ""
    assert emitted["name"] == ""
    assert emitted["note"] == ""


def test_blob_csv_expand_blob_arm_emits_a_row_for_a_line_of_empty_values() -> None:
    """The BLOB arm draws the empty-row/empty-file line in the same place.

    John's own distinction, stated against CSV: a file with headings and ``,,,``
    is a row with no data and is FINE; a completely empty file is a fail state.
    The sibling test below pins the inline arm; the blob arm is the one the
    ``empty_csv`` error lives on, so it is the one most at risk of a later
    "simplification" widening that error into a general emptiness check and
    silently discarding real rows.
    """
    body = b"id,name,note\n,,\n"
    blob_ref = _hash(body)
    transform = _build_transform()
    transform._payload_store = _PayloadStoreFake({blob_ref: body})

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "blob_ref": blob_ref}),
        make_context(),
    )

    assert result.status == "success", result.reason
    assert result.rows is not None
    assert len(result.rows) == 1
    assert [result.rows[0].to_dict()[key] for key in ("id", "name", "note")] == ["", "", ""]
    assert result.rows[0].to_dict()["csv_row_index"] == 0


def test_blob_csv_expand_field_arm_emits_a_row_for_all_empty_values_too() -> None:
    """The inline arm draws the empty-row/empty-file line in the same place."""
    transform = _build_field_transform()

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "content": "id,name,note\n,,\n"}),
        make_context(),
    )

    assert result.status == "success"
    assert result.rows is not None
    assert len(result.rows) == 1
    assert [result.rows[0].to_dict()[key] for key in ("id", "name", "note")] == ["", "", ""]


# ── the inline arm consumes its source column ────────────────────────────────


def test_blob_csv_expand_field_arm_drops_the_consumed_text_column() -> None:
    """The source document must not be copied onto every emitted row.

    A 5,905-char document expanding to 500 rows would otherwise write 2,952,500
    characters — into the audit payload store, not only memory. line_explode and
    json_explode consume a row field the same way and drop it for the same
    reason.
    """
    transform = _build_field_transform()

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "content": CSV_TEXT}),
        make_context(),
    )

    assert result.status == "success"
    assert result.rows is not None
    assert [row.to_dict() for row in result.rows] == [
        {"url": "https://example.test/a.csv", "id": "1", "name": "alice", "csv_row_index": 0},
        {"url": "https://example.test/a.csv", "id": "2", "name": "bob", "csv_row_index": 1},
    ]
    assert all("content" not in row.to_dict() for row in result.rows)


def test_blob_csv_expand_field_arm_keeps_every_other_upstream_field() -> None:
    """Dropping the consumed column must not hide UNRELATED upstream columns.

    This is the exact defect ``forwards_input_fields`` was added for
    (elspeth-15c72686f2): a transform that forwards the row minus one column was
    invisible to the guarantee/emit walks, which stopped at it and hid upstream
    fields from a locked (extra=forbid) downstream consumer — the pipeline built
    green and every row died at the consumer's preflight.
    """
    transform = _build_field_transform()

    result = transform.process(
        make_pipeline_row(
            {
                "url": "https://example.test/a.csv",
                "record_id": "r1",
                "response_usage": 42,
                "content": CSV_TEXT,
            }
        ),
        make_context(),
    )

    assert result.status == "success"
    assert result.rows is not None
    for row in result.rows:
        emitted = row.to_dict()
        assert emitted["url"] == "https://example.test/a.csv"
        assert emitted["record_id"] == "r1"
        assert emitted["response_usage"] == 42
        assert "content" not in emitted


def test_blob_csv_expand_field_arm_declares_the_removal_it_performs() -> None:
    """The declaration and the behaviour must agree, arm by arm.

    ``passes_through_input`` is all-or-nothing and enforced per row by the
    executor with no ``removed_input_fields`` exemption, so an arm that drops a
    column MUST swap to the forwards/removed pair or every row raises a TIER_1
    PassThroughContractViolation.
    """
    field_transform = _build_field_transform()

    assert field_transform.passes_through_input is False
    assert field_transform.forwards_input_fields is True
    assert field_transform.removed_input_fields == frozenset({"content"})


def test_blob_csv_expand_blob_arm_keeps_the_full_pass_through_promise() -> None:
    """The blob arm reads a hash, not the document, so nothing is consumed."""
    body = b"id,name\n1,alice\n"
    blob_ref = _hash(body)
    transform = _build_transform()
    transform._payload_store = _PayloadStoreFake({blob_ref: body})

    assert transform.passes_through_input is True
    assert transform.forwards_input_fields is False
    assert transform.removed_input_fields == frozenset()

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "blob_ref": blob_ref}),
        make_context(),
    )

    assert result.status == "success"
    assert result.rows is not None
    assert result.rows[0].to_dict()["blob_ref"] == blob_ref


def test_blob_csv_expand_instance_removal_does_not_mutate_the_class_declaration() -> None:
    """The swap is per-INSTANCE: one inline node must not disarm the blob arm."""
    _build_field_transform()

    assert BlobCSVExpand.passes_through_input is True
    assert _build_transform().passes_through_input is True
    assert _build_transform().removed_input_fields == frozenset()


def test_blob_csv_expand_field_arm_still_reports_a_header_colliding_with_the_text_column() -> None:
    """The column is dropped AFTER the collision check, which is deliberate.

    A CSV header named like the text column is a genuine collision on the row
    that arrived, even though that column will not survive to the output. Doing
    the removal first would silently accept a document that overwrites its own
    source field.
    """
    transform = _build_field_transform()

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "content": "content,name\nx,alice\n"}),
        make_context(),
    )

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "field_collision"
    assert result.reason["fields"] == ["content"]


def test_blob_csv_expand_field_arm_output_schema_stops_advertising_the_consumed_column() -> None:
    """Dropping the column from the ROWS is not enough — the schema must agree.

    ``_output_schema_config`` is what build-time DAG validation compares against
    the downstream consumer. Leaving the consumed column in it makes the graph
    promise a column no emitted row carries, and a ``mode: fixed`` sink then
    refuses to BUILD with "Extra fields forbidden by consumer" — naming a field
    the author cannot legally declare, because declaring it fails too. Caught by
    the examples corpus, not by a plugin test; this is that missing test.
    """
    field_transform = _build_field_transform()
    schema_fields = {field.name for field in field_transform._output_schema_config.fields or ()}
    guaranteed = set(field_transform._output_schema_config.guaranteed_fields or ())

    assert "content" not in schema_fields
    assert "content" not in guaranteed
    assert "content" not in field_transform.output_schema.model_fields

    # The blob arm removes nothing. Asserted against a schema that DECLARES the
    # locator, because the shared observed fixture declares no fields at all and
    # would make this pass vacuously for either arm.
    declared = {
        "mode": "fixed",
        "fields": [
            {"name": "blob_ref", "field_type": "str", "required": True},
            {"name": "content", "field_type": "str", "required": True},
        ],
        "guaranteed_fields": ["blob_ref", "content"],
    }
    blob_schema = _build_transform(schema=declared)._output_schema_config
    blob_fields = {field.name for field in blob_schema.fields or ()}
    assert {"blob_ref", "content"} <= blob_fields
    assert {"blob_ref", "content"} <= set(blob_schema.guaranteed_fields or ())

    # Same declared schema on the inline arm: only the consumed column goes.
    inline_schema = _build_field_transform(schema=declared)._output_schema_config
    inline_fields = {field.name for field in inline_schema.fields or ()}
    assert "content" not in inline_fields
    assert "content" not in set(inline_schema.guaranteed_fields or ())
    assert "blob_ref" in inline_fields


# ── F1: the ceiling boundary itself ──────────────────────────────────────────


def test_blob_csv_expand_accepts_exactly_max_output_rows() -> None:
    """A blob with EXACTLY ``max_output_rows`` data rows must SUCCEED.

    Every other ceiling test feeds input strictly OVER the limit, so mutating
    ``row_number > self._max_output_rows`` to ``>=`` survives them all — and the
    consequence is a legal blob at the exact ceiling being quarantined, i.e.
    rows killed by a config the author wrote correctly. This pins the boundary
    from the accepting side, which is the side no other test covers.
    """
    body = b"id,name\n1,alice\n2,bob\n3,carol\n"
    blob_ref = _hash(body)
    transform = _build_transform(max_output_rows=3)
    transform._payload_store = _PayloadStoreFake({blob_ref: body})

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "blob_ref": blob_ref}),
        make_context(),
    )

    assert result.status == "success"
    assert result.rows is not None
    assert len(result.rows) == 3
    assert [row.to_dict()["name"] for row in result.rows] == ["alice", "bob", "carol"]


def test_blob_csv_expand_rejects_one_row_over_max_output_rows() -> None:
    """The refusing side of the same boundary, one row further along.

    Paired with the test above so the two together pin the exact transition
    point rather than "somewhere above the limit".
    """
    body = b"id,name\n1,alice\n2,bob\n3,carol\n4,dan\n"
    blob_ref = _hash(body)
    transform = _build_transform(max_output_rows=3)
    transform._payload_store = _PayloadStoreFake({blob_ref: body})

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "blob_ref": blob_ref}),
        make_context(),
    )

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "too_many_rows"
    assert result.reason["max_output_rows"] == 3


def test_blob_csv_expand_field_arm_shares_the_ceiling_boundary() -> None:
    """The bound is applied in the shared parser, so both arms transition together."""
    at_ceiling = _build_field_transform(max_output_rows=3)
    over_ceiling = _build_field_transform(max_output_rows=3)

    ok = at_ceiling.process(
        make_pipeline_row({"url": "u", "content": "id,name\n1,a\n2,b\n3,c\n"}),
        make_context(),
    )
    refused = over_ceiling.process(
        make_pipeline_row({"url": "u", "content": "id,name\n1,a\n2,b\n3,c\n4,d\n"}),
        make_context(),
    )

    assert ok.status == "success"
    assert ok.rows is not None
    assert len(ok.rows) == 3
    assert refused.status == "error"
    assert refused.reason is not None
    assert refused.reason["reason"] == "too_many_rows"


# ── F3: a corrupt payload must crash, never route ────────────────────────────


def test_blob_csv_expand_propagates_integrity_error_rather_than_routing_it() -> None:
    """A corrupt payload is not a missing payload.

    ``IntegrityError`` means the store's bytes no longer match their hash.
    Quarantining that as a routed row error would let the run continue past
    evidence of store corruption, so it must escape ``process()`` entirely
    rather than become a ``blob_not_found`` reason.
    """

    class _CorruptPayloadStore:
        def retrieve(self, content_hash: str) -> bytes:
            raise IntegrityError(f"checksum mismatch for {content_hash}")

    blob_ref = _hash(b"id,name\n1,alice\n")
    transform = _build_transform()
    transform._payload_store = _CorruptPayloadStore()

    with pytest.raises(IntegrityError):
        transform.process(
            make_pipeline_row({"url": "https://example.test/a.csv", "blob_ref": blob_ref}),
            make_context(),
        )


def test_blob_csv_expand_still_routes_a_genuinely_missing_blob() -> None:
    """The control for the test above: a MISSING blob is still a routed row error.

    Without this, a mutation that re-raised everything would pass the integrity
    test while destroying the quarantine path that ordinary missing blobs need.
    """
    blob_ref = _hash(b"id,name\n1,alice\n")
    transform = _build_transform()
    transform._payload_store = FilesystemPayloadStore(Path(tempfile.mkdtemp()) / "payloads")

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.csv", "blob_ref": blob_ref}),
        make_context(),
    )

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "blob_not_found"


def test_blob_csv_expand_integrity_wins_when_an_error_is_both_kinds() -> None:
    """The ORDER of the two except clauses, tested behaviourally.

    ``IntegrityError`` and ``PayloadNotFoundError`` are unrelated siblings
    today, so simply deleting the ``except IntegrityError: raise`` clause
    changes nothing that a test can observe — Python propagates it either way.
    That makes the ordering untestable by the obvious route, and an untested
    ordering is one a future refactor can silently invert.

    A store raising an error that is BOTH kinds resolves it without waiting for
    the hierarchy to change: handlers match in order, so integrity-first
    propagates (correct — corruption must crash) while
    ``blob_not_found``-first would swallow it into a routed row error. This is
    the one shape that distinguishes the two orderings using only the classes
    that exist today.
    """

    class _CorruptAndMissingError(IntegrityError, PayloadNotFoundError):
        """Both kinds at once — the shape a future hierarchy change would create."""

    class _AmbiguousPayloadStore:
        def retrieve(self, content_hash: str) -> bytes:
            raise _CorruptAndMissingError(f"corrupt and missing: {content_hash}")

    blob_ref = _hash(b"id,name\n1,alice\n")
    transform = _build_transform()
    transform._payload_store = _AmbiguousPayloadStore()

    with pytest.raises(IntegrityError):
        transform.process(
            make_pipeline_row({"url": "https://example.test/a.csv", "blob_ref": blob_ref}),
            make_context(),
        )
