"""Tests for blob_json_expand transform."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.payload_store import IntegrityError, PayloadNotFoundError
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.validation import validate_transform_config
from elspeth.plugins.transforms.blob_json_expand import BlobJSONExpand
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context

DYNAMIC_SCHEMA = {"mode": "observed", "guaranteed_fields": ["url", "blob_ref", "blob_content_type"]}
DOCUMENT_FIELDS = ["document_id", "title", "sections"]


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
    """Minimal LifecycleContext double — mirrors test_blob_csv_expand.py's."""

    def __init__(self, payload_store: Any) -> None:
        self.payload_store = payload_store


class _IntegrityFailingPayloadStore:
    def retrieve(self, content_hash: str) -> bytes:
        raise IntegrityError(f"stored payload for {content_hash} failed its own hash check")


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _build_transform(**overrides: Any) -> BlobJSONExpand:
    config: dict[str, Any] = {
        "schema": DYNAMIC_SCHEMA,
        "blob_ref_field": "blob_ref",
        "fields": list(DOCUMENT_FIELDS),
    }
    config.update(overrides)
    return BlobJSONExpand(config)


def _blob_row(blob_ref: str, *, content_type: str = "application/json", **extra: Any) -> Any:
    row = {"url": "https://example.test/a.json", "blob_ref": blob_ref, "blob_content_type": content_type}
    row.update(extra)
    return make_pipeline_row(row)


def _run_blob(body: bytes, *, content_type: str = "application/json", **overrides: Any) -> Any:
    blob_ref = _hash(body)
    transform = _build_transform(**overrides)
    transform._payload_store = _PayloadStoreFake({blob_ref: body})
    return transform.process(_blob_row(blob_ref, content_type=content_type), make_context())


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
        # PayloadNotFoundError.__init__ requires a non-empty content_hash;
        # delegate explicitly rather than relying on MRO resolution.
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
    blob_ref = _hash(b"corrupt")

    with pytest.raises(IntegrityError):
        transform.process(_blob_row(blob_ref), make_context())


def test_integrity_ordering_still_routes_a_genuinely_missing_blob(tmp_path: Path) -> None:
    """The control for the ordering test above.

    Without it, a mutation that re-raised EVERYTHING from the retrieve seam
    would satisfy the integrity test while destroying the routing path that a
    purged or stale payload depends on.

    Uses a real ``FilesystemPayloadStore`` rather than ``_PayloadStoreFake``:
    the fake raises a bare ``KeyError`` on a miss, which never reaches the
    ``PayloadNotFoundError`` handler this control exists to exercise.
    """
    transform = _build_transform()
    transform._payload_store = FilesystemPayloadStore(tmp_path / "payloads")

    result = transform.process(_blob_row(_hash(b"never stored")), make_context())

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "blob_not_found"


# ── Happy paths ─────────────────────────────────────────────────────────────


def test_data_key_selects_the_record_array_and_upstream_fields_survive() -> None:
    """One row per record; the originating url stays on every emitted row."""
    body = json.dumps(
        {
            "documents": [
                {"document_id": "d1", "title": "First", "sections": ["a", "b"]},
                {"document_id": "d2", "title": "Second", "sections": ["c"]},
            ]
        }
    ).encode()

    result = _run_blob(body, data_key="documents")

    assert result.status == "success"
    assert result.is_multi_row
    assert result.rows is not None
    assert [row.to_dict() for row in result.rows] == [
        {
            "url": "https://example.test/a.json",
            "blob_ref": _hash(body),
            "blob_content_type": "application/json",
            "document_id": "d1",
            "title": "First",
            "sections": ["a", "b"],
            "json_record_index": 0,
        },
        {
            "url": "https://example.test/a.json",
            "blob_ref": _hash(body),
            "blob_content_type": "application/json",
            "document_id": "d2",
            "title": "Second",
            "sections": ["c"],
            "json_record_index": 1,
        },
    ]


def test_top_level_array_is_read_when_data_key_is_omitted() -> None:
    """Without data_key the document's top level must itself be the array."""
    body = json.dumps([{"document_id": "d1", "title": "First", "sections": []}]).encode()

    result = _run_blob(body)

    assert result.status == "success"
    assert result.rows is not None
    assert [row.to_dict()["document_id"] for row in result.rows] == ["d1"]


def test_nested_list_survives_as_a_real_sequence_that_json_explode_accepts() -> None:
    """The spec's central decision: nested values are real Python values.

    Not asserted by shape alone — the emitted row is fed to the real
    json_explode, which refuses anything that is not list-shaped. That is the
    whole reason the projection exists.
    """
    from elspeth.plugins.transforms.json_explode import JSONExplode

    body = json.dumps({"documents": [{"document_id": "d1", "title": "First", "sections": [{"heading": "a"}, {"heading": "b"}]}]}).encode()

    result = _run_blob(body, data_key="documents")

    assert result.rows is not None
    (expanded,) = result.rows
    # to_dict() thaws the deep-frozen row back to real containers.
    assert isinstance(expanded.to_dict()["sections"], list)
    assert expanded.to_dict()["sections"] == [{"heading": "a"}, {"heading": "b"}]

    exploder = JSONExplode({"schema": {"mode": "observed"}, "array_field": "sections", "output_field": "section"})
    chained = exploder.process(expanded, make_context())

    assert chained.status == "success", chained.reason
    assert chained.rows is not None
    assert [row.to_dict()["section"] for row in chained.rows] == [{"heading": "a"}, {"heading": "b"}]


def test_jsonl_format_reads_one_record_per_line() -> None:
    body = b'{"document_id": "d1", "title": "First", "sections": []}\n\n{"document_id": "d2", "title": "Second", "sections": []}\n'

    result = _run_blob(body, content_type="application/x-ndjson")

    assert result.status == "success", result.reason
    assert result.rows is not None
    assert [row.to_dict()["document_id"] for row in result.rows] == ["d1", "d2"]
    assert [row.to_dict()["json_record_index"] for row in result.rows] == [0, 1]


def test_records_with_differing_extra_keys_do_not_crash_the_multi_row_path() -> None:
    """Undeclared record keys are dropped so emitted rows stay homogeneous.

    A heterogeneous emitted key set is a bare ValueError in the multi-row path,
    not a quarantine, and JSON records legitimately differ record to record.
    """
    body = json.dumps(
        {
            "documents": [
                {"document_id": "d1", "title": "First", "sections": [], "author": "ann"},
                {"document_id": "d2", "title": "Second", "sections": [], "reviewer": "bo"},
            ]
        }
    ).encode()

    result = _run_blob(body, data_key="documents")

    assert result.status == "success", result.reason
    assert result.rows is not None
    assert [sorted(row.to_dict()) for row in result.rows] == [
        sorted(["url", "blob_ref", "blob_content_type", "document_id", "title", "sections", "json_record_index"])
    ] * 2
    assert all("author" not in row.to_dict() and "reviewer" not in row.to_dict() for row in result.rows)


def test_record_keys_are_normalized_to_lowercase_identifiers() -> None:
    body = json.dumps({"documents": [{"Document ID": "d1", "TITLE": "First", "sections": []}]}).encode()

    result = _run_blob(body, data_key="documents")

    assert result.status == "success", result.reason
    assert result.rows is not None
    assert result.rows[0].to_dict()["document_id"] == "d1"


def test_field_mapping_overrides_a_normalized_record_key() -> None:
    body = json.dumps({"documents": [{"docid": "d1", "title": "First", "sections": []}]}).encode()

    result = _run_blob(body, data_key="documents", field_mapping={"docid": "document_id"})

    assert result.status == "success", result.reason
    assert result.rows is not None
    assert result.rows[0].to_dict()["document_id"] == "d1"


# ── Empty container vs empty row ────────────────────────────────────────────


def test_an_empty_record_array_is_a_fail_state() -> None:
    """Zero rows cannot be used for anything downstream: the empty CONTAINER errors.

    Distinct from the empty ROW below, which is data. Mirrors the CSV sibling's
    call for an empty CSV (``blob_csv_expand.py:351-360``). Supersedes design
    spec line 134 ("empty array — zero emitted rows, audited, not an error"),
    which is WRONG, not merely overridden — John's ruling 2026-08-26.
    """
    body = json.dumps({"documents": []}).encode()

    result = _run_blob(body, data_key="documents")

    assert result.status == "error"
    assert result.rows is None
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["error_type"] == "empty_document"
    assert result.reason["blob_ref"] == _hash(body)
    assert BlobJSONExpand.can_drop_rows is False


@pytest.mark.parametrize(
    ("label", "record"),
    [
        ("empty strings and list", {"document_id": "", "title": "", "sections": []}),
        ("nulls", {"document_id": None, "title": None, "sections": None}),
    ],
)
def test_a_record_whose_declared_fields_are_all_empty_still_emits_a_row(label: str, record: dict[str, Any]) -> None:
    """The empty ROW is data — the JSON equivalent of a CSV ``,,,`` line.

    This is the test that pins the distinction. A container holding no records
    is a failure to produce data; a record holding empty values IS data and must
    survive. "Simplifying" the empty-container error into a general emptiness
    check would silently discard real rows.
    """
    del label
    body = json.dumps({"documents": [record]}).encode()

    result = _run_blob(body, data_key="documents")

    assert result.status == "success", result.reason
    assert result.rows is not None
    assert len(result.rows) == 1
    emitted = result.rows[0].to_dict()
    assert emitted["document_id"] == record["document_id"]
    assert emitted["title"] == record["title"]
    assert emitted["sections"] == record["sections"]
    assert emitted["json_record_index"] == 0


# ── The consumed text column is dropped, everything else forwarded ──────────


def test_the_field_arm_consumes_its_text_column_instead_of_copying_the_document() -> None:
    """A fan-out that forwarded its source document would persist it N times.

    ``passes_through_input`` cannot express this — it promises EVERY input field
    survives — so the declaration is ``forwards_input_fields`` plus an explicit
    ``removed_input_fields`` (line_explode.py:342-349). The other upstream
    columns MUST still reach the consumer: hiding them is the exact defect
    ``forwards_input_fields`` was added for (elspeth-15c72686f2).
    """
    document = json.dumps({"documents": [{"document_id": "d1", "title": "First", "sections": ["a"]}]})
    transform = BlobJSONExpand(
        {
            "schema": {"mode": "observed", "guaranteed_fields": ["url", "content", "run_label"]},
            "source": "field",
            "format": "json",
            "data_key": "documents",
            "fields": list(DOCUMENT_FIELDS),
        }
    )

    # The declaration TRIPLE. passes_through_input and forwards_input_fields are
    # ALTERNATIVES, not layers: the executor's verifier
    # (pass_through.py:109) honours no removed_input_fields exemption, so
    # leaving pass-through True while dropping a column raises a TIER_1
    # PassThroughContractViolation on every inline row. process() cannot see
    # that — the verifier runs in the EXECUTOR — so this assertion, not a green
    # row-shape test, is what catches it.
    assert transform.passes_through_input is False
    assert transform.forwards_input_fields is True
    assert transform.removed_input_fields == frozenset({"content"})

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/a.json", "run_label": "batch-7", "content": document}),
        make_context(),
    )

    assert result.status == "success", result.reason
    assert result.rows is not None
    emitted = result.rows[0].to_dict()
    assert "content" not in emitted
    # The OTHER input columns survive — the half the declaration exists for.
    assert emitted["url"] == "https://example.test/a.json"
    assert emitted["run_label"] == "batch-7"


def test_the_document_is_not_duplicated_across_a_fan_out() -> None:
    """The consequence the removal exists for, measured rather than asserted structurally."""
    records = [{"document_id": f"d{index}", "title": "T", "sections": []} for index in range(20)]
    document = json.dumps({"documents": records})
    transform = BlobJSONExpand(
        {
            "schema": {"mode": "observed", "guaranteed_fields": ["content"]},
            "source": "field",
            "format": "json",
            "data_key": "documents",
            "fields": list(DOCUMENT_FIELDS),
        }
    )

    result = transform.process(make_pipeline_row({"content": document}), make_context())

    assert result.rows is not None
    assert len(result.rows) == 20
    assert not any("content" in row.to_dict() for row in result.rows)
    # Without the removal every row would carry the whole document.
    assert sum(len(json.dumps(row.to_dict())) for row in result.rows) < len(document) * 20


def test_the_output_schema_stops_advertising_the_consumed_column() -> None:
    """Dropping the column from emitted rows alone is NOT enough.

    ``_output_schema_config`` is what the executor checks each emitted row
    against and what build-time DAG validation shows the consumer. Leaving the
    consumed column declared there makes the node promise a column no row
    carries: the run dies with SchemaConfigModeViolation ("missing required
    fields") on the first row. Caught only by a real engine run —
    ``examples/blob_transforms/settings_expand_inline_json.yaml`` — which is
    why it is asserted here directly.
    """
    inline = BlobJSONExpand(
        {
            "schema": {"mode": "fixed", "fields": ["record_id: str", "content: str"], "guaranteed_fields": ["record_id", "content"]},
            "source": "field",
            "format": "json",
            "fields": list(DOCUMENT_FIELDS),
        }
    )

    declared = {field.name for field in inline._output_schema_config.fields or ()}
    assert "content" not in declared
    assert "content" not in set(inline._output_schema_config.guaranteed_fields or ())
    # The OTHER declared input column is still promised.
    assert "record_id" in declared


def test_the_blob_arm_output_schema_keeps_every_input_field() -> None:
    """The blob arm consumes nothing, so it must advertise everything it forwards."""
    blob = _build_transform(
        schema={"mode": "fixed", "fields": ["blob_ref: str", "content: str"], "guaranteed_fields": ["blob_ref", "content"]},
        format="json",
    )

    declared = {field.name for field in blob._output_schema_config.fields or ()}
    assert {"blob_ref", "content"} <= declared


def test_the_blob_arm_claims_no_removal() -> None:
    """The removal set is arm-aware: the blob arm never reads a text column.

    Over-stating removals is safe and under-stating is not, but claiming one the
    arm does not make would be a false statement about this node's output.
    """
    transform = _build_transform()

    assert transform.passes_through_input is True
    assert transform.forwards_input_fields is False
    assert transform.removed_input_fields == frozenset()


def test_an_inline_instance_does_not_disarm_pass_through_for_blob_instances() -> None:
    """The swap is per-INSTANCE. A class-body assignment would disarm every sibling.

    ``passes_through_input`` is a class attribute; rebinding it on ``self``
    shadows it for one instance. Writing to the class instead would silently
    drop the TIER-1 guarantee for every blob-arm node in the process.
    """
    inline = BlobJSONExpand(
        {
            "schema": {"mode": "observed", "guaranteed_fields": ["content"]},
            "source": "field",
            "format": "json",
            "fields": list(DOCUMENT_FIELDS),
        }
    )

    assert inline.passes_through_input is False
    assert BlobJSONExpand.passes_through_input is True
    assert _build_transform().passes_through_input is True


def test_the_text_column_can_never_be_a_declared_field_so_the_removal_masks_nothing() -> None:
    """Two guards meet here, and the interaction is what makes the removal safe.

    Dropping the consumed column AFTER the collision check is deliberate — a
    record key colliding with an input field must still be caught on the row
    that ARRIVED. For the text column specifically the case is unreachable:
    naming it in ``fields`` is refused at CONFIG time by the created-field
    guard, so the removal cannot mask a collision it would otherwise report.
    """
    with pytest.raises(PluginConfigError) as excinfo:
        BlobJSONExpand(
            {
                "schema": {"mode": "observed", "guaranteed_fields": ["content"]},
                "source": "field",
                "format": "json",
                "fields": ["content", "title", "sections"],
            }
        )

    assert "text_field" in str(excinfo.value)
    assert "content" in str(excinfo.value)


def test_the_inline_arm_still_reports_collisions_against_other_input_fields() -> None:
    """The removal is scoped to the consumed column; every other collision stands."""
    document = json.dumps({"documents": [{"url": "https://other.test", "title": "T", "sections": []}]})
    transform = BlobJSONExpand(
        {
            "schema": {"mode": "observed", "guaranteed_fields": ["url", "content"]},
            "source": "field",
            "format": "json",
            "data_key": "documents",
            "fields": ["url", "title", "sections"],
        }
    )

    result = transform.process(make_pipeline_row({"url": "https://example.test/a.json", "content": document}), make_context())

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "field_collision"
    assert result.reason["fields"] == ["url"]


def test_the_blob_arm_still_forwards_its_locator() -> None:
    """blob_ref is a short locator, not the document — it stays on every row."""
    body = json.dumps([{"document_id": "d1", "title": "T", "sections": []}]).encode()

    result = _run_blob(body)

    assert result.rows is not None
    assert result.rows[0].to_dict()["blob_ref"] == _hash(body)


# ── source: field arm ───────────────────────────────────────────────────────


def test_source_field_parses_json_text_from_a_row_field_with_no_payload_store() -> None:
    """The gap json_explode names: a JSON-looking STRING now has a parser."""
    transform = BlobJSONExpand(
        {
            "schema": {"mode": "observed", "guaranteed_fields": ["url", "content"]},
            "source": "field",
            "format": "json",
            "text_field": "content",
            "data_key": "documents",
            "fields": list(DOCUMENT_FIELDS),
        }
    )

    result = transform.process(
        make_pipeline_row(
            {
                "url": "https://example.test/a.json",
                "content": json.dumps({"documents": [{"document_id": "d1", "title": "First", "sections": ["a"]}]}),
            }
        ),
        make_context(),
    )

    assert result.status == "success", result.reason
    assert result.rows is not None
    assert result.rows[0].to_dict()["document_id"] == "d1"
    assert result.rows[0].to_dict()["sections"] == ["a"]
    assert not hasattr(transform, "_payload_store")


def test_source_field_starts_without_a_payload_store() -> None:
    """The field arm never touches the payload store, so it must not demand one.

    The guard is arm-conditional: inverted, every source='field' pipeline would
    die at startup with FrameworkBugError.
    """
    transform = BlobJSONExpand(
        {
            "schema": {"mode": "observed", "guaranteed_fields": ["content"]},
            "source": "field",
            "format": "json",
            "fields": list(DOCUMENT_FIELDS),
        }
    )

    transform.on_start(_FakeLifecycleContext(None))

    assert not hasattr(transform, "_payload_store")


def test_source_blob_refuses_to_start_without_a_payload_store() -> None:
    transform = _build_transform()

    with pytest.raises(FrameworkBugError):
        transform.on_start(_FakeLifecycleContext(None))


def test_source_blob_binds_the_payload_store_it_was_given() -> None:
    transform = _build_transform()
    store = _PayloadStoreFake({})

    transform.on_start(_FakeLifecycleContext(store))

    assert transform._payload_store is store


def test_source_field_declares_text_field_not_blob_ref_as_its_input() -> None:
    transform = BlobJSONExpand(
        {
            "schema": {"mode": "observed", "guaranteed_fields": ["content"]},
            "source": "field",
            "format": "json",
            "fields": list(DOCUMENT_FIELDS),
        }
    )

    assert "content" in transform.declared_input_fields
    assert "blob_ref" not in transform.declared_input_fields


def test_source_field_quarantines_a_non_string_text_field() -> None:
    transform = BlobJSONExpand(
        {
            "schema": {"mode": "observed", "guaranteed_fields": ["content"]},
            "source": "field",
            "format": "json",
            "fields": list(DOCUMENT_FIELDS),
        }
    )

    result = transform.process(make_pipeline_row({"content": 17}), make_context())

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "type_mismatch"
    assert result.reason["field"] == "content"


# ── Format inference, fail-closed ───────────────────────────────────────────


def test_content_type_field_is_a_declared_input_only_while_format_is_inferred() -> None:
    """Absent content type is caught at BUILD time, via the declared input contract.

    process() cannot raise PluginConfigError for a row value, so the spec's
    "config-time error" for an ABSENT content type is enforced here instead: a
    pipeline whose upstream cannot guarantee the column is rejected before rows
    flow.
    """
    inferring = _build_transform()
    explicit = _build_transform(format="json")

    assert "blob_content_type" in inferring.declared_input_fields
    assert "blob_content_type" not in explicit.declared_input_fields
    assert "blob_ref" in explicit.declared_input_fields


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("application/json", b'[{"document_id": "d1", "title": "T", "sections": []}]'),
        ("application/jsonl", b'{"document_id": "d1", "title": "T", "sections": []}\n'),
        ("application/x-ndjson", b'{"document_id": "d1", "title": "T", "sections": []}\n'),
        ("text/jsonl", b'{"document_id": "d1", "title": "T", "sections": []}\n'),
    ],
)
def test_recognised_content_types_select_a_format(content_type: str, body: bytes) -> None:
    """A jsonl body under a json content type would fail to parse, and vice versa."""
    result = _run_blob(body, content_type=content_type)

    assert result.status == "success", result.reason
    assert result.rows is not None
    assert result.rows[0].to_dict()["document_id"] == "d1"


@pytest.mark.parametrize("content_type", ["text/plain", "text/csv", "application/xml", ""])
def test_unrecognised_content_type_is_refused_with_format_as_the_remedy(content_type: str) -> None:
    body = json.dumps([{"document_id": "d1", "title": "First", "sections": []}]).encode()

    result = _run_blob(body, content_type=content_type)

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "unsupported_content_type"
    assert "format" in result.reason["error"]


def test_explicit_format_overrides_an_unrecognised_content_type() -> None:
    body = json.dumps([{"document_id": "d1", "title": "First", "sections": []}]).encode()

    result = _run_blob(body, content_type="text/plain", format="json")

    assert result.status == "success", result.reason


def test_content_type_parameters_are_stripped_before_lookup() -> None:
    body = json.dumps([{"document_id": "d1", "title": "First", "sections": []}]).encode()

    result = _run_blob(body, content_type="application/json; charset=utf-8")

    assert result.status == "success", result.reason


# ── Value-level faults ──────────────────────────────────────────────────────


def test_record_missing_a_declared_field_is_a_value_level_error() -> None:
    """Heterogeneous records are caught at the boundary, not several nodes later."""
    body = json.dumps({"documents": [{"document_id": "d1", "title": "First", "sections": []}, {"document_id": "d2"}]}).encode()

    result = _run_blob(body, data_key="documents")

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "missing_field"
    assert result.reason["fields"] == ["title", "sections"]
    assert result.reason["row_number"] == 2


def test_non_object_array_element_is_a_value_level_error() -> None:
    body = json.dumps({"documents": [{"document_id": "d1", "title": "T", "sections": []}, "not-an-object"]}).encode()

    result = _run_blob(body, data_key="documents")

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["error_type"] == "record_not_object"
    assert result.reason["actual"] == "str"
    assert result.reason["row_number"] == 2


def test_declared_field_colliding_with_an_input_field_is_a_value_level_error() -> None:
    body = json.dumps({"documents": [{"url": "https://other.test", "title": "T", "sections": []}]}).encode()

    transform = _build_transform(data_key="documents", fields=["url", "title", "sections"])
    transform._payload_store = _PayloadStoreFake({_hash(body): body})

    result = transform.process(_blob_row(_hash(body)), make_context())

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "field_collision"
    assert result.reason["fields"] == ["url"]


def test_record_index_field_colliding_with_an_input_field_is_a_value_level_error() -> None:
    body = json.dumps({"documents": [{"document_id": "d1", "title": "T", "sections": []}]}).encode()

    transform = _build_transform(data_key="documents")
    transform._payload_store = _PayloadStoreFake({_hash(body): body})

    result = transform.process(_blob_row(_hash(body), json_record_index=99), make_context())

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "field_collision"
    assert result.reason["fields"] == ["json_record_index"]


def test_two_record_keys_normalizing_to_one_name_is_a_value_level_error() -> None:
    body = json.dumps({"documents": [{"Document ID": "d1", "document_id": "d2", "title": "T", "sections": []}]}).encode()

    result = _run_blob(body, data_key="documents")

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "field_collision"
    assert result.reason["fields"] == ["document_id"]


def test_exactly_max_output_rows_is_accepted() -> None:
    """The BOUNDARY, which no over-the-limit test can pin.

    The ceiling guard is ``>``. Every other ceiling test feeds input strictly
    over the limit, so mutating it to ``>=`` survives them all — and a blob
    holding exactly ``max_output_rows`` records would then be quarantined: a
    legal document refused, with the suite still green.
    """
    records = [{"document_id": f"d{index}", "title": "T", "sections": []} for index in range(3)]
    body = json.dumps({"documents": records}).encode()

    result = _run_blob(body, data_key="documents", max_output_rows=3)

    assert result.status == "success", result.reason
    assert result.rows is not None
    assert len(result.rows) == 3
    assert [row.to_dict()["document_id"] for row in result.rows] == ["d0", "d1", "d2"]


def test_max_output_rows_is_enforced_per_document() -> None:
    body = json.dumps({"documents": [{"document_id": f"d{i}", "title": "T", "sections": []} for i in range(3)]}).encode()

    result = _run_blob(body, data_key="documents", max_output_rows=2)

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "too_many_rows"
    assert result.reason["row_count"] == 3
    assert result.reason["max_output_rows"] == 2


def test_max_blob_bytes_is_enforced_before_decode() -> None:
    body = json.dumps({"documents": [{"document_id": "d1", "title": "T", "sections": []}]}).encode()

    result = _run_blob(body, data_key="documents", max_blob_bytes=8)

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "blob_too_large"
    assert result.reason["max_blob_bytes"] == 8
    assert result.reason["body_size"] == len(body)


def test_malformed_json_is_a_value_level_error() -> None:
    result = _run_blob(b'{"documents": [')

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_json"
    assert result.reason["phase"] == "document"


def test_data_key_with_an_inferred_jsonl_format_is_refused_rather_than_ignored() -> None:
    """Config time cannot see an INFERRED format, so the clash must be caught per row.

    The explicit `format: jsonl` + data_key combination is a config rejection.
    This is the reachable sibling: format omitted, the content type resolves to
    jsonl, and data_key would otherwise be silently discarded — wrong rows, no
    error, no audit trace.
    """
    body = b'{"document_id": "d1", "title": "T", "sections": []}\n'

    result = _run_blob(body, content_type="application/x-ndjson", data_key="documents")

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["error_type"] == "data_key_with_jsonl"
    assert "data_key" in result.reason["error"]
    assert "format" in result.reason["error"]


def test_malformed_jsonl_line_names_the_line_number() -> None:
    body = b'{"document_id": "d1", "title": "T", "sections": []}\nnot-json\n'

    result = _run_blob(body, content_type="application/x-ndjson")

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_json"
    assert result.reason["phase"] == "jsonl_line"
    assert result.reason["line_number"] == 2


def test_undecodable_bytes_are_a_value_level_error() -> None:
    result = _run_blob(b"\xff\xfe\x00[")

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "decode_failed"
    assert result.reason["encoding"] == "utf-8"


def test_missing_data_key_lists_the_available_keys() -> None:
    body = json.dumps({"payload": []}).encode()

    result = _run_blob(body, data_key="documents")

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["error_type"] == "data_key_not_found"
    assert result.reason["available_fields"] == ["payload"]


def test_data_key_pointing_at_a_non_array_is_a_value_level_error() -> None:
    body = json.dumps({"documents": {"document_id": "d1"}}).encode()

    result = _run_blob(body, data_key="documents")

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["error_type"] == "records_not_array"
    assert result.reason["actual"] == "dict"


def test_data_key_against_a_non_object_root_is_a_value_level_error() -> None:
    body = json.dumps([{"document_id": "d1"}]).encode()

    result = _run_blob(body, data_key="documents")

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["error_type"] == "data_key_root_not_object"
    assert result.reason["actual"] == "list"


def test_top_level_non_array_without_data_key_is_a_value_level_error() -> None:
    body = json.dumps({"document_id": "d1"}).encode()

    result = _run_blob(body)

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["error_type"] == "records_not_array"


def test_missing_blob_is_a_value_level_error(tmp_path: Path) -> None:
    body = json.dumps([{"document_id": "d1", "title": "T", "sections": []}]).encode()
    transform = _build_transform()
    transform._payload_store = FilesystemPayloadStore(tmp_path / "payloads")

    result = transform.process(_blob_row(_hash(body)), make_context())

    assert result.status == "error"
    assert result.retryable is False
    assert result.reason is not None
    assert result.reason["reason"] == "blob_not_found"
    assert result.reason["field"] == "blob_ref"


def test_malformed_blob_ref_is_a_value_level_error(tmp_path: Path) -> None:
    transform = _build_transform()
    transform._payload_store = FilesystemPayloadStore(tmp_path / "payloads")

    result = transform.process(_blob_row("not-a-sha256"), make_context())

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["error_type"] == "invalid_blob_ref"
    assert result.reason["blob_ref"] == "not-a-sha256"


def test_payload_store_integrity_failure_crashes_rather_than_quarantines() -> None:
    """Tier 1: ELSPETH's own store returning corrupt bytes is not bad external data.

    The design spec's value-level list names integrity failure, but the sibling
    line it cites as the convention (blob_csv_expand.py:307) re-raises. The
    cited authority wins: a payload whose bytes fail their own hash means the
    store is corrupt, which must stop the run.
    """
    transform = _build_transform()
    transform._payload_store = _IntegrityFailingPayloadStore()

    with pytest.raises(IntegrityError):
        transform.process(_blob_row("0" * 64), make_context())


# ── Config-time rejections ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fields",
    [[], ["document_id", "document_id"], ["not an identifier"], ["class"], [""]],
    ids=["empty", "duplicate", "non-identifier", "keyword", "blank"],
)
def test_invalid_fields_are_rejected_at_config_time_naming_the_option(fields: list[str]) -> None:
    with pytest.raises(PluginConfigError) as excinfo:
        _build_transform(fields=fields)

    assert "fields" in str(excinfo.value)


def test_record_index_field_colliding_with_blob_ref_field_is_rejected() -> None:
    with pytest.raises(PluginConfigError) as excinfo:
        _build_transform(record_index_field="blob_ref")

    assert "blob_ref_field" in str(excinfo.value)


def test_record_index_field_colliding_with_a_declared_field_is_rejected() -> None:
    with pytest.raises(PluginConfigError) as excinfo:
        _build_transform(record_index_field="title")

    assert "record_index_field" in str(excinfo.value)


def test_record_index_field_colliding_with_text_field_is_rejected_on_the_field_arm() -> None:
    with pytest.raises(PluginConfigError) as excinfo:
        _build_transform(source="field", format="json", record_index_field="content")

    assert "text_field" in str(excinfo.value)


def test_source_field_without_format_is_rejected_naming_format() -> None:
    with pytest.raises(PluginConfigError) as excinfo:
        _build_transform(source="field")

    assert "format" in str(excinfo.value)


def test_data_key_with_explicit_jsonl_is_rejected() -> None:
    with pytest.raises(PluginConfigError) as excinfo:
        _build_transform(format="jsonl", data_key="documents")

    assert "data_key" in str(excinfo.value)


def test_unknown_format_is_rejected() -> None:
    with pytest.raises(PluginConfigError):
        _build_transform(format="yaml")


def test_fields_is_required() -> None:
    with pytest.raises(PluginConfigError) as excinfo:
        BlobJSONExpand({"schema": DYNAMIC_SCHEMA})

    assert "fields" in str(excinfo.value)


def test_success_audit_names_the_arm_and_the_document_it_expanded() -> None:
    body = json.dumps([{"document_id": "d1", "title": "T", "sections": []}]).encode()

    result = _run_blob(body)

    assert result.success_reason["action"] == "expanded_blob"
    assert result.success_reason["fields_added"] == ["document_id", "json_record_index", "sections", "title"]
    assert result.success_reason["metadata"] == {
        "blob_ref": _hash(body),
        "field": "blob_ref",
        "source": "blob",
        "row_count": 1,
    }


def test_parse_error_carries_the_blob_it_came_from() -> None:
    """A quarantined row must say WHICH document failed, not just that one did."""
    body = b'{"documents": ['

    result = _run_blob(body, data_key="documents")

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["blob_ref"] == _hash(body)
    assert result.reason["field"] == "blob_ref"


# ── Corpus witness ──────────────────────────────────────────────────────────


def test_reads_the_multi_doc_sections_fixture_and_chains_into_json_explode() -> None:
    """The shipped fixture is read UNMODIFIED; if the plugin cannot read it, the plugin is wrong.

    This is the whole target chain minus the network and the LLM:
    blob_json_expand fans one document out per record, and json_explode then
    fans each record's `sections` list out per section. It only composes
    because `sections` arrives as a real sequence.
    """
    from elspeth.plugins.transforms.json_explode import JSONExplode

    fixture = Path(__file__).resolve().parents[4] / "website/tutorial-site/multi-doc-sections.json"
    body = fixture.read_bytes()

    result = _run_blob(body, data_key="documents")

    assert result.status == "success", result.reason
    assert result.rows is not None
    documents = [row.to_dict() for row in result.rows]
    assert len(documents) >= 2
    assert documents[0]["document_id"] == "doc-helios"
    assert [document["json_record_index"] for document in documents] == list(range(len(documents)))
    # The fixture's top-level "_notice" key is not a record and must not leak in.
    assert all(set(document) == set(documents[0]) for document in documents)
    assert "_notice" not in documents[0]

    exploder = JSONExplode({"schema": {"mode": "observed"}, "array_field": "sections", "output_field": "section"})
    sections = exploder.process(result.rows[0], make_context())

    assert sections.status == "success", sections.reason
    assert sections.rows is not None
    assert len(sections.rows) == len(documents[0]["sections"])
    assert all(isinstance(row.to_dict()["section"], str) for row in sections.rows)


# ── Declarations ────────────────────────────────────────────────────────────


def test_declared_output_fields_cover_every_declared_field_plus_the_index() -> None:
    transform = _build_transform()

    assert transform.declared_output_fields == frozenset({"document_id", "title", "sections", "json_record_index"})


def _build_bare(**options: Any) -> BlobJSONExpand:
    """Construct WITHOUT the shared helper's explicit blob_ref_field.

    ``_build_transform`` always writes ``blob_ref_field``, which is exactly the
    authored-vs-defaulted distinction these tests turn on.
    """
    config: dict[str, Any] = {"schema": {"mode": "observed"}, "fields": ["docbody", "title"]}
    config.update(options)
    return BlobJSONExpand(config)


@pytest.mark.parametrize(
    "options",
    [
        pytest.param({"source": "field", "format": "json", "blob_ref_field": "docbody"}, id="field-arm-blob-ref-field"),
        pytest.param({"source": "blob", "format": "json", "blob_ref_field": "docbody"}, id="blob-arm-blob-ref-field"),
        pytest.param({"source": "blob", "content_type_field": "docbody"}, id="blob-arm-content-type-field"),
        pytest.param({"source": "field", "format": "json", "text_field": "docbody"}, id="field-arm-text-field"),
    ],
)
def test_an_input_option_naming_a_declared_field_is_rejected(options: dict[str, Any]) -> None:
    """An input locator aimed at a member of `fields` kills every row, silently.

    Nothing else catches the shape: the executor's collision check needs a row
    to actually carry the column, and ``mode: observed`` declares nothing for
    DAG validation to carry. The field would land in ``consumed_input_fields``,
    never demote, and stay required on input — every row rejected for missing
    what the transform exists to create (elspeth-09dc6407f1).
    """
    # CONTROL FIRST: the same option pointed at a column that ARRIVES must
    # construct, so the rejection below is attributable to the created-field-ness
    # rather than to the option having been touched at all.
    _build_bare(**{**options, next(key for key in options if key.endswith("_field")): "arriving_column"})

    with pytest.raises(PluginConfigError) as excinfo:
        _build_bare(**options)

    message = str(excinfo.value)
    assert "docbody" in message
    assert "creates" in message

    # BOTH paths must agree. validate_transform_config never CONSTRUCTS the
    # transform, so a guard living only in __init__ makes pre-validation report
    # a config valid that the engine then rejects. It RETURNS errors, it does
    # not raise.
    config: dict[str, Any] = {"schema": {"mode": "observed"}, "fields": ["docbody", "title"], **options}
    errors = validate_transform_config("blob_json_expand", config)
    assert len(errors) == 1, errors
    assert "docbody" in errors[0].message


@pytest.mark.parametrize("created_field", ["docbody", "title", "json_record_index"])
def test_every_created_field_is_guarded_not_only_the_first(created_field: str) -> None:
    """The guarded set is the FULL created set — every `fields` member AND the index.

    A guard covering only the scalar created name reproduces the defect: the
    list-valued half is the part that was unguarded. Derived from
    ``_blob_json_added_output_fields``, so a new emitted field cannot escape it.
    """
    with pytest.raises(PluginConfigError) as excinfo:
        _build_bare(format="json", blob_ref_field=created_field)

    assert created_field in str(excinfo.value)
    assert (
        len(
            validate_transform_config(
                "blob_json_expand",
                {"schema": {"mode": "observed"}, "fields": ["docbody", "title"], "format": "json", "blob_ref_field": created_field},
            )
        )
        == 1
    )


def test_an_unwritten_blob_ref_field_cannot_kill_a_declared_field_on_the_field_arm() -> None:
    """The silent half: a config the author never wrote must not consume a column.

    ``source: field`` never reads a blob reference. With ``blob_ref_field``
    str-defaulted, its default reached ``consumed_input_fields`` anyway — so an
    author who chose the field arm, never typed ``blob_ref_field``, and happened
    to declare a ``blob_ref`` field had every row killed by a config they did
    not write. None names no column.
    """
    transform = _build_bare(source="field", format="json", fields=["blob_ref", "title"])

    assert "blob_ref" not in transform.consumed_input_fields
    assert "blob_ref" in transform.self_created_input_fields
    assert not set(transform.self_created_input_fields) & set(transform.consumed_input_fields)


def test_the_blob_arm_still_consumes_its_locator_without_being_told() -> None:
    """The None default must not silently stop the blob arm reading blob_ref."""
    transform = _build_bare()

    assert transform._blob_ref_field == "blob_ref"
    assert "blob_ref" in transform.consumed_input_fields


def test_blob_arm_does_not_acquire_a_text_column_requirement() -> None:
    """The inline arm's option must not un-demote a declared field on the blob arm.

    ``consumed_input_fields`` folds in the value of every ``*_field`` option —
    read from the VALIDATED config, so an option the author never wrote still
    contributes its default — and ``input_schema`` demotes only created fields
    that are NOT consumed. With ``text_field`` defaulting to the string
    ``content``, a blob config declaring a ``content`` field stopped demoting it
    and rejected every row for missing the field the transform exists to create,
    from a config that never mentions ``source`` (elspeth-d6eeb3a71d).
    """
    transform = _build_transform(
        schema={
            "mode": "fixed",
            "fields": [
                {"name": "blob_ref", "field_type": "str", "required": True},
                {"name": "blob_content_type", "field_type": "str", "required": True},
                {"name": "content", "field_type": "any", "required": True},
            ],
        },
        fields=["content"],
    )

    assert "content" not in transform.consumed_input_fields
    assert "content" in transform.demoted_input_fields
    assert not transform.input_schema.model_fields["content"].is_required()
    transform.input_schema.model_validate({"blob_ref": "r", "blob_content_type": "application/json"}, strict=True)
    assert "content" not in _build_transform().consumed_input_fields


def test_content_type_field_is_not_consumed_when_the_format_is_explicit() -> None:
    """The same leak, one option over: an unread column must not be claimed as read."""
    inferring = _build_transform()
    explicit = _build_transform(format="json")

    assert "blob_content_type" in inferring.consumed_input_fields
    assert "blob_content_type" not in explicit.consumed_input_fields


def test_field_arm_defaults_the_text_field_to_the_shared_spelling() -> None:
    """``source: field`` with no ``text_field`` still reads the family-wide column."""
    transform = _build_transform(source="field", format="json", text_field=None)

    assert "content" in transform.declared_input_fields
    assert "content" in transform.consumed_input_fields


def test_an_authored_text_field_is_consumed_on_either_arm() -> None:
    """None means "names nothing", not "never named" — an explicit name still counts."""
    transform = _build_transform(text_field="doc_body")

    assert transform._text_field == "doc_body"
    assert "doc_body" in transform.consumed_input_fields


def test_declared_fields_are_not_required_on_input() -> None:
    """`fields` names columns this transform CREATES, so no row must supply them.

    `is_column_naming_config_option` matches the bare name "fields", so without
    it in output_naming_config_keys every entry is classified as a column the
    transform READS and stays required on the derived input model — a contract
    no arriving row can satisfy.
    """
    transform = _build_transform(
        schema={"mode": "flexible", "fields": ["document_id: any", "title: any", "sections: any", "json_record_index: any"]}
    )

    required = {name for name, field in transform.input_schema.model_fields.items() if field.is_required()}

    assert required.isdisjoint({"document_id", "title", "sections", "json_record_index"})
    assert "fields" in BlobJSONExpand.output_naming_config_keys
    transform.input_schema.model_validate({}, strict=True)


def test_include_record_index_defaults_true_so_the_plugin_stays_probeable() -> None:
    """An empty self_created_input_fields makes the plugin unprobeable."""
    transform = _build_transform()

    assert transform.self_created_input_fields
    assert "json_record_index" in transform.declared_output_fields


def test_include_record_index_false_drops_the_index_column() -> None:
    body = json.dumps([{"document_id": "d1", "title": "T", "sections": []}]).encode()

    result = _run_blob(body, include_record_index=False)

    assert result.status == "success", result.reason
    assert result.rows is not None
    assert "json_record_index" not in result.rows[0].to_dict()


def test_discovery_registers_blob_json_expand() -> None:
    from elspeth.plugins.infrastructure.manager import PluginManager

    manager = PluginManager()
    manager.register_builtin_plugins()

    transform = manager.get_transform_by_name("blob_json_expand")
    assert transform.name == "blob_json_expand"
