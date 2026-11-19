"""Inline blob content resolver tests."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from elspeth.contracts.blobs import (
    BlobContentMissingError,
    BlobIntegrityError,
    BlobNotFoundError,
    BlobRecord,
    BlobStateError,
)
from elspeth.contracts.blobs_inline import (
    BlobContentResolutionError,
    BlobInlineRef,
    BlobInlineValidationViolation,
    InlineBlobNotTextError,
    ResolvedBlobContent,
)
from elspeth.contracts.enums import CreationModality
from elspeth.core.blobs_inline import (
    _discover_blob_content_refs,
    _enforce_blob_content_ref_metadata,
    _evaluate_blob_content_ref_metadata,
    _resolve_blob_content_results,
    _substitute_blob_content_refs,
    _unique_blob_ids,
    _validate_blob_content_refs_sync,
)

VALID_HASH = "a" * 64
BLOB1 = "5b7a4e0e-9e4a-4f0b-8d3e-2c0e1f0d3a4b"
BLOB2 = "7c3a4e0e-9e4a-4f0b-8d3e-2c0e1f0d3aaa"
BLOB3 = "9d4a4e0e-9e4a-4f0b-8d3e-2c0e1f0d3bbb"
SESSION_ID = UUID("11111111-1111-4111-8111-111111111111")


def _marker(blob_id: str = BLOB1, sha256: str = VALID_HASH) -> dict[str, str]:
    return {"blob_ref": blob_id, "mode": "inline_content", "sha256": sha256}


def _ref(blob_id: str = BLOB1, field_path: str = "source.options.x") -> BlobInlineRef:
    return BlobInlineRef(field_path=field_path, blob_id=UUID(blob_id), sha256=VALID_HASH, encoding="utf-8")


def _ref_with_hash(content: bytes, field_path: str = "source.options.system_prompt", blob_id: str = BLOB1) -> BlobInlineRef:
    return BlobInlineRef(
        field_path=field_path,
        blob_id=UUID(blob_id),
        sha256=hashlib.sha256(content).hexdigest(),
        encoding="utf-8",
    )


def _blob_record(
    *,
    blob_id: str = BLOB1,
    session_id: UUID = SESSION_ID,
    status: str = "ready",
    content_hash: str | None = VALID_HASH,
    size_bytes: int = 12,
) -> BlobRecord:
    return BlobRecord(
        id=UUID(blob_id),
        session_id=session_id,
        filename="prompt.txt",
        mime_type="text/plain",
        size_bytes=size_bytes,
        content_hash=content_hash,
        storage_path="/tmp/prompt.txt",
        created_at=datetime(2026, 5, 24, tzinfo=UTC),
        created_by="user",
        source_description=None,
        status=status,  # type: ignore[arg-type]
        creation_modality=CreationModality.VERBATIM,
        created_from_message_id=None,
        creating_model_identifier=None,
        creating_model_version=None,
        creating_provider=None,
        creating_composer_skill_hash=None,
        creating_arguments_hash=None,
    )


def test_non_text_metadata_is_a_typed_refusal_before_content_read() -> None:
    ref = _ref()
    record = replace(_blob_record(), mime_type="application/pdf")
    config = {"source": {"options": {"x": _marker()}}}

    violations = _validate_blob_content_refs_sync(lambda _blob_id: record, config)
    assert violations == [
        BlobInlineValidationViolation(category="not_text", field_path=ref.field_path, detail="blob MIME is not inline text")
    ]
    with pytest.raises(InlineBlobNotTextError) as caught:
        _enforce_blob_content_ref_metadata([ref], {ref.blob_id: record})
    assert caught.value.error_code == "inline_blob_not_text"


def test_direct_materialization_refuses_non_text_mime_before_audit_row() -> None:
    content = b"PDF shaped bytes"
    ref = _ref_with_hash(content)
    config = {"source": {"options": {"system_prompt": _marker(sha256=ref.sha256)}}}

    with pytest.raises(InlineBlobNotTextError) as caught:
        _substitute_blob_content_refs(
            config,
            {ref: content},
            refs=[ref],
            blob_metadata={ref.blob_id: ("application/pdf", len(content))},
        )

    assert caught.value.error_code == "inline_blob_not_text"
    assert config["source"]["options"]["system_prompt"] == _marker(sha256=ref.sha256)


class TestDiscoverBlobContentRefs:
    def test_discovers_source_option_refs(self) -> None:
        config = {"source": {"plugin": "csv", "options": {"system_prompt": _marker()}}}

        refs = _discover_blob_content_refs(config)

        assert len(refs) == 1
        assert refs[0].field_path == "source.options.system_prompt"
        assert refs[0].blob_id == UUID(BLOB1)
        assert refs[0].sha256 == VALID_HASH
        assert refs[0].encoding == "utf-8"

    def test_discovers_plural_source_option_refs_with_source_name(self) -> None:
        config = {
            "sources": {
                "orders": {"plugin": "csv", "options": {"system_prompt": _marker(BLOB1)}},
                "refunds": {"plugin": "json", "options": {"template": _marker(BLOB2)}},
            }
        }

        refs = _discover_blob_content_refs(config)

        assert {ref.field_path for ref in refs} == {
            "source:orders.options.system_prompt",
            "source:refunds.options.template",
        }

    def test_discovers_node_option_refs_across_node_collections(self) -> None:
        config = {
            "transforms": [
                {"name": "classify", "plugin": "llm", "options": {"system_prompt": _marker(BLOB1)}},
            ],
            "gates": [
                {"name": "policy-check", "plugin": "expression", "options": {"template": _marker(BLOB2)}},
            ],
            "aggregations": [
                {"name": "rollup", "plugin": "group_by", "options": {}},
            ],
            "coalesce": [
                {"name": "merge", "plugin": "first", "options": {"explain": _marker(BLOB1)}},
            ],
        }

        refs = _discover_blob_content_refs(config)

        assert {ref.field_path for ref in refs} == {
            "node:classify.options.system_prompt",
            "node:policy-check.options.template",
            "node:merge.options.explain",
        }

    def test_discovers_output_and_sink_refs_with_same_canonical_prefix(self) -> None:
        config = {
            "outputs": {
                "state-view": {"plugin": "json", "options": {"body_template": _marker(BLOB1)}},
            },
            "sinks": {
                "writeback": {"plugin": "csv", "options": {"footer_template": _marker(BLOB2)}},
            },
        }

        refs = _discover_blob_content_refs(config)

        assert {ref.field_path for ref in refs} == {
            "output:state-view.options.body_template",
            "output:writeback.options.footer_template",
        }

    def test_discovers_nested_refs_inside_options(self) -> None:
        config = {
            "source": {
                "plugin": "csv",
                "options": {
                    "auth": {
                        "system_prompt": _marker(),
                    },
                },
            },
        }

        refs = _discover_blob_content_refs(config)

        assert len(refs) == 1
        assert refs[0].field_path == "source.options.auth.system_prompt"

    def test_ignores_bind_source_refs_and_secret_refs(self) -> None:
        config = {
            "source": {
                "plugin": "csv",
                "options": {
                    "blob_ref": BLOB1,
                    "mode": "bind_source",
                    "path": "/tmp/input.csv",
                    "api_key": {"secret_ref": "OPENROUTER_KEY"},
                },
            }
        }

        assert _discover_blob_content_refs(config) == []

    @pytest.mark.parametrize("source_name", [None, "orders", "orders.options.archive"])
    def test_source_plugin_encoding_does_not_hide_prompt_refs(self, source_name: str | None) -> None:
        content = "Original prompt\r\n{{ row.text }}\nEnd é".encode()
        source_options = {
            "blob_ref": BLOB1,
            "mode": "bind_source",
            "path": "inputs/source.csv",
            "encoding": "utf-8-sig",
            "schema": {"mode": "observed"},
        }
        source = {"plugin": "csv", "options": source_options}
        config: dict[str, Any] = {
            "transforms": [
                {"name": "assess", "plugin": "llm", "options": {"system_prompt": _marker(BLOB2, hashlib.sha256(content).hexdigest())}}
            ]
        }
        if source_name is None:
            config["source"] = source
        else:
            config["sources"] = {source_name: source}
        original = deepcopy(config)

        refs = _discover_blob_content_refs(config)

        assert [ref.field_path for ref in refs] == ["node:assess.options.system_prompt"]
        assert config == original
        assert (
            _validate_blob_content_refs_sync(
                lambda blob_id: _blob_record(blob_id=str(blob_id), content_hash=hashlib.sha256(content).hexdigest()), config
            )
            == []
        )
        resolved, _audit = _substitute_blob_content_refs(
            config, {refs[0]: content}, refs=refs, blob_metadata={refs[0].blob_id: ("text/plain", len(content))}
        )
        assert resolved["transforms"][0]["options"]["system_prompt"].encode() == content
        if source_name is None:
            assert resolved["source"]["options"] == original["source"]["options"]
        else:
            assert resolved["sources"][source_name]["options"] == original["sources"][source_name]["options"]

    @pytest.mark.parametrize("extra", [{"encoding": "utf-8"}, {"sha256": VALID_HASH}])
    @pytest.mark.parametrize("source_name", [None, "orders"])
    def test_nested_bind_source_marker_still_rejects_inline_fields(self, extra: dict[str, str], source_name: str | None) -> None:
        marker = {"blob_ref": BLOB1, "mode": "bind_source", **extra}
        source = {"plugin": "csv", "options": {"payload": {"options": marker}}}
        config = {"source": source} if source_name is None else {"sources": {source_name: source}}
        prefix = "source" if source_name is None else f"source:{source_name}"

        with pytest.raises(BlobContentResolutionError) as exc_info:
            _discover_blob_content_refs(config)

        assert exc_info.value.malformed == ((f"{prefix}.options.payload.options", "bind_source cannot carry sha256 or encoding"),)

    def test_nested_source_options_are_not_treated_as_root_source_options(self) -> None:
        config = {
            "sources": {
                "orders": {
                    "plugin": "csv",
                    "options": {"payload": {"options": {"blob_ref": BLOB1, "mode": "bind_source", "template": _marker(BLOB2)}}},
                }
            }
        }

        with pytest.raises(BlobContentResolutionError) as exc_info:
            _discover_blob_content_refs(config)

        assert exc_info.value.malformed == (("source:orders.options.payload.options", "invalid blob_ref marker"),)

    def test_emits_one_ref_per_field_path_even_for_same_blob(self) -> None:
        config = {
            "transforms": [
                {"name": "a", "plugin": "llm", "options": {"system_prompt": _marker(BLOB1)}},
                {"name": "b", "plugin": "llm", "options": {"system_prompt": _marker(BLOB1)}},
            ]
        }

        refs = _discover_blob_content_refs(config)

        assert len(refs) == 2
        assert {ref.field_path for ref in refs} == {
            "node:a.options.system_prompt",
            "node:b.options.system_prompt",
        }

    def test_malformed_markers_raise_batched_error(self) -> None:
        config = {"source": {"plugin": "csv", "options": {"system_prompt": {"blob_ref": BLOB1}}}}

        with pytest.raises(BlobContentResolutionError) as exc_info:
            _discover_blob_content_refs(config)

        assert exc_info.value.malformed == (("source.options.system_prompt", "missing mode"),)

    @pytest.mark.parametrize(
        ("marker", "reason"),
        [
            ({"blob_ref": BLOB1, "mode": []}, "mode must be string"),
            ({"blob_ref": BLOB1, "mode": "inline_content", "sha256": VALID_HASH, "encoding": []}, "encoding must be string"),
        ],
    )
    def test_non_string_marker_fields_are_malformed(self, marker: dict[str, object], reason: str) -> None:
        config = {"source": {"plugin": "csv", "options": {"system_prompt": marker}}}

        with pytest.raises(BlobContentResolutionError) as exc_info:
            _discover_blob_content_refs(config)

        assert exc_info.value.malformed == (("source.options.system_prompt", reason),)

    def test_discovers_markers_inside_lists_at_exact_positions(self) -> None:
        config = {"source": {"plugin": "csv", "options": {"prompts": [_marker()]}}}

        assert [ref.field_path for ref in _discover_blob_content_refs(config)] == ["source.options.prompts.0"]

    def test_discovers_nested_markers_inside_lists_at_exact_positions(self) -> None:
        config = {"source": {"plugin": "csv", "options": {"prompts": [{"prompt": _marker()}]}}}

        assert [ref.field_path for ref in _discover_blob_content_refs(config)] == ["source.options.prompts.0.prompt"]

    def test_malformed_marker_in_list_is_not_skipped(self) -> None:
        config = {"source": {"plugin": "csv", "options": {"prompts": [{"blob_ref": BLOB1}]}}}
        with pytest.raises(BlobContentResolutionError) as exc_info:
            _discover_blob_content_refs(config)
        assert exc_info.value.malformed[0][0] == "source.options.prompts.0"

    @pytest.mark.parametrize("key", ["0", "2024", "v1.2", "a/b"])
    def test_marker_under_a_key_that_is_not_a_path_name_is_refused(self, key: str) -> None:
        """A numeric or dotted mapping key would yield a path naming a different value."""
        config = {"transforms": [{"name": "n", "plugin": "llm", "options": {"queries": {key: {"template": _marker()}}}}]}
        with pytest.raises(BlobContentResolutionError) as exc_info:
            _discover_blob_content_refs(config)
        assert [path for path, _reason in exc_info.value.malformed] == [f"node:n.options.queries.{key}"]

    def test_key_that_is_not_a_path_name_is_ignored_without_a_marker(self) -> None:
        config = {"transforms": [{"name": "n", "plugin": "llm", "options": {"queries": {"2024": {"template": "Plain text"}}}}]}
        assert _discover_blob_content_refs(config) == []


@pytest.mark.parametrize("wrong_hash", [False, True])
def test_list_query_resolution_preserves_text_order_and_checks_hash(wrong_hash: bool) -> None:
    content = "Original prompt\r\n{{ row.text }}\nEnd é".encode()
    ref = _ref_with_hash(content, "node:assess.options.queries.1.template")
    config = {
        "transforms": [
            {
                "name": "assess",
                "plugin": "llm",
                "options": {
                    "queries": [
                        {"name": "first", "template": "First exact text"},
                        {"name": "second", "template": {"blob_ref": BLOB1, "mode": "inline_content", "sha256": ref.sha256}},
                    ]
                },
            }
        ]
    }
    original = deepcopy(config)
    refs = _discover_blob_content_refs(config)
    assert refs == [ref]
    if wrong_hash:
        with pytest.raises(BlobIntegrityError):
            _substitute_blob_content_refs(
                config, {ref: b"Changed text"}, refs=refs, blob_metadata={ref.blob_id: ("text/plain", len(content))}
            )
        return
    resolved, audit = _substitute_blob_content_refs(
        config, {ref: content}, refs=refs, blob_metadata={ref.blob_id: ("text/plain", len(content))}
    )
    queries = resolved["transforms"][0]["options"]["queries"]
    assert [q["name"] for q in queries] == ["first", "second"]
    assert queries[0]["template"] == "First exact text"
    assert queries[1]["template"].encode() == content
    assert audit[0].field_path == ref.field_path
    assert original["transforms"][0]["options"]["queries"][1]["template"]["sha256"] == ref.sha256


class TestResolveBlobContentResults:
    """The production read batch: ``_unique_blob_ids`` dedupes the reads and
    ``_resolve_blob_content_results`` classifies one gathered batch of
    ``read_blob_content`` outcomes (``ExecutionService._read_inline_blob_contents``).

    These cover what the deleted async ``_fetch_blob_contents`` pair covered
    (P4-Q5): the pair had no production caller, so its behaviour is pinned at
    the helpers the production loop actually calls.
    """

    def test_returns_bytes_by_ref(self) -> None:
        ref = _ref()
        unique = _unique_blob_ids([ref])

        assert unique == [UUID(BLOB1)]
        assert _resolve_blob_content_results([ref], unique, [b"content"]) == {ref: b"content"}

    def test_dedupes_reads_by_blob_id(self) -> None:
        refs = [
            _ref(BLOB1, "source.options.a"),
            _ref(BLOB1, "source.options.b"),
        ]
        unique = _unique_blob_ids(refs)

        # One read per blob id, fanned back out to every ref that named it.
        assert unique == [UUID(BLOB1)]
        assert _resolve_blob_content_results(refs, unique, [b"content"]) == {refs[0]: b"content", refs[1]: b"content"}

    def test_propagates_integrity_errors(self) -> None:
        ref = _ref()
        failure = BlobIntegrityError(BLOB1, expected="a" * 64, actual="b" * 64)

        with pytest.raises(BlobIntegrityError) as exc_info:
            _resolve_blob_content_results([ref], [UUID(BLOB1)], [failure])

        assert exc_info.value is failure

    def test_propagates_content_missing_errors(self) -> None:
        ref = _ref()
        failure = BlobContentMissingError(BLOB1, storage_path="/tmp/blob.csv")

        with pytest.raises(BlobContentMissingError) as exc_info:
            _resolve_blob_content_results([ref], [UUID(BLOB1)], [failure])

        assert exc_info.value is failure

    def test_collects_not_found_by_field_path(self) -> None:
        ref = _ref()

        with pytest.raises(BlobContentResolutionError) as exc_info:
            _resolve_blob_content_results([ref], [UUID(BLOB1)], [BlobNotFoundError(BLOB1)])

        assert exc_info.value.missing == ("source.options.x",)

    def test_collects_not_ready_by_field_path(self) -> None:
        ref = _ref()

        with pytest.raises(BlobContentResolutionError) as exc_info:
            _resolve_blob_content_results([ref], [UUID(BLOB1)], [BlobStateError(BLOB1, message="Blob is pending")])

        assert exc_info.value.not_ready == (("source.options.x", "Blob is pending"),)


class TestSubstituteBlobContentRefs:
    def test_replaces_marker_with_decoded_string_and_audit_record(self) -> None:
        content = b"You are a helpful assistant."
        ref = _ref_with_hash(content)
        config = {
            "source": {
                "options": {
                    "system_prompt": {
                        "blob_ref": BLOB1,
                        "mode": "inline_content",
                        "sha256": ref.sha256,
                    }
                }
            }
        }

        substituted, audit = _substitute_blob_content_refs(
            deepcopy(config),
            {ref: content},
            refs=[ref],
            blob_metadata={ref.blob_id: ("text/plain", len(content))},
        )

        assert substituted["source"]["options"]["system_prompt"] == "You are a helpful assistant."
        assert audit == [
            ResolvedBlobContent(
                field_path="source.options.system_prompt",
                blob_id=UUID(BLOB1),
                content_hash=ref.sha256,
                byte_length=len(content),
                mime_type="text/plain",
                encoding="utf-8",
            )
        ]

    def test_replaces_plural_source_marker_with_decoded_string_and_named_audit_path(self) -> None:
        content = b"You are a helpful assistant."
        ref = _ref_with_hash(content, "source:orders.options.system_prompt")
        config = {
            "sources": {
                "orders": {
                    "options": {
                        "system_prompt": {
                            "blob_ref": BLOB1,
                            "mode": "inline_content",
                            "sha256": ref.sha256,
                        }
                    }
                }
            }
        }

        substituted, audit = _substitute_blob_content_refs(
            deepcopy(config),
            {ref: content},
            refs=[ref],
            blob_metadata={ref.blob_id: ("text/plain", len(content))},
        )

        assert substituted["sources"]["orders"]["options"]["system_prompt"] == "You are a helpful assistant."
        assert audit == [
            ResolvedBlobContent(
                field_path="source:orders.options.system_prompt",
                blob_id=UUID(BLOB1),
                content_hash=ref.sha256,
                byte_length=len(content),
                mime_type="text/plain",
                encoding="utf-8",
            )
        ]

    def test_replaces_node_and_output_paths(self) -> None:
        node_content = b"Classify strictly."
        output_content = b"Footer text"
        node_ref = _ref_with_hash(node_content, "node:classify.options.system_prompt", BLOB1)
        output_ref = _ref_with_hash(output_content, "output:writeback.options.footer_template", BLOB2)
        config = {
            "transforms": [
                {
                    "name": "classify",
                    "options": {
                        "system_prompt": {
                            "blob_ref": BLOB1,
                            "mode": "inline_content",
                            "sha256": node_ref.sha256,
                        }
                    },
                }
            ],
            "sinks": {
                "writeback": {
                    "options": {
                        "footer_template": {
                            "blob_ref": BLOB2,
                            "mode": "inline_content",
                            "sha256": output_ref.sha256,
                        }
                    }
                }
            },
        }

        substituted, audit = _substitute_blob_content_refs(
            deepcopy(config),
            {node_ref: node_content, output_ref: output_content},
            refs=[node_ref, output_ref],
            blob_metadata={
                node_ref.blob_id: ("text/plain", len(node_content)),
                output_ref.blob_id: ("text/plain", len(output_content)),
            },
        )

        assert substituted["transforms"][0]["options"]["system_prompt"] == "Classify strictly."
        assert substituted["sinks"]["writeback"]["options"]["footer_template"] == "Footer text"
        assert [entry.field_path for entry in audit] == [
            "node:classify.options.system_prompt",
            "output:writeback.options.footer_template",
        ]

    def test_hash_mismatch_propagates_integrity_error(self) -> None:
        content = b"You are a helpful assistant."
        ref = BlobInlineRef(
            field_path="source.options.system_prompt",
            blob_id=UUID(BLOB1),
            sha256="b" * 64,
            encoding="utf-8",
        )
        config = {"source": {"options": {"system_prompt": _marker(BLOB1, ref.sha256)}}}

        with pytest.raises(BlobIntegrityError):
            _substitute_blob_content_refs(
                config,
                {ref: content},
                refs=[ref],
                blob_metadata={ref.blob_id: ("text/plain", len(content))},
            )

    def test_decode_failures_are_batched_without_mutating_config(self) -> None:
        content = b"\xff\xfe\xfd"
        ref = BlobInlineRef(
            field_path="source.options.x",
            blob_id=UUID(BLOB1),
            sha256=hashlib.sha256(content).hexdigest(),
            encoding="utf-8",
        )
        config = {"source": {"options": {"x": _marker(BLOB1, ref.sha256)}}}
        original = deepcopy(config)

        with pytest.raises(BlobContentResolutionError) as exc_info:
            _substitute_blob_content_refs(
                config,
                {ref: content},
                refs=[ref],
                blob_metadata={ref.blob_id: ("text/plain", len(content))},
            )

        assert exc_info.value.undecodable == (("source.options.x", "utf-8"),)
        assert config == original


class TestValidateBlobContentRefs:
    def test_metadata_evaluator_collects_shared_policy_facts(self) -> None:
        oversized = _blob_record(blob_id=BLOB1, content_hash=VALID_HASH, size_bytes=128)
        pending = _blob_record(blob_id=BLOB2, status="pending")
        mismatch = _blob_record(blob_id=BLOB3, content_hash="b" * 64)
        refs = [
            _ref(BLOB1, "source.options.oversized"),
            _ref(BLOB2, "source.options.pending"),
            _ref(BLOB3, "source.options.mismatch"),
            _ref("aaaaaaaa-9e4a-4f0b-8d3e-2c0e1f0d3ccc", "source.options.missing"),
        ]

        result = _evaluate_blob_content_ref_metadata(
            refs,
            {
                UUID(BLOB1): oversized,
                UUID(BLOB2): pending,
                UUID(BLOB3): mismatch,
            },
            per_ref_byte_cap=64,
            aggregate_byte_cap=64,
        )

        assert result.missing == (("source.options.missing", UUID("aaaaaaaa-9e4a-4f0b-8d3e-2c0e1f0d3ccc")),)
        assert result.not_ready == (("source.options.pending", f"blob {UUID(BLOB2)} status is 'pending'"),)
        assert result.hash_mismatches == ((refs[2], "b" * 64),)
        assert result.oversized == (
            ("source.options.oversized", 128, 64),
            ("(aggregate)", 128, 64),
        )

    def test_runtime_metadata_enforcement_batches_shared_non_integrity_errors(self) -> None:
        oversized = _blob_record(blob_id=BLOB1, content_hash=VALID_HASH, size_bytes=128)
        pending = _blob_record(blob_id=BLOB2, status="pending")
        refs = [
            _ref(BLOB1, "source.options.oversized"),
            _ref(BLOB2, "source.options.pending"),
            _ref(BLOB3, "source.options.missing"),
        ]

        with pytest.raises(BlobContentResolutionError) as exc_info:
            _enforce_blob_content_ref_metadata(
                refs,
                {
                    UUID(BLOB1): oversized,
                    UUID(BLOB2): pending,
                },
                per_ref_byte_cap=64,
                aggregate_byte_cap=64,
            )

        assert exc_info.value.missing == ("source.options.missing",)
        assert exc_info.value.not_ready == (("source.options.pending", f"blob {UUID(BLOB2)} status is 'pending'"),)
        assert exc_info.value.oversized == (
            ("source.options.oversized", 128, 64),
            ("(aggregate)", 128, 64),
        )

    def test_sync_returns_malformed_violation_without_lookup(self) -> None:
        looked_up: list[UUID] = []

        def get_metadata(blob_id: UUID) -> BlobRecord | None:
            looked_up.append(blob_id)
            return None

        config = {"source": {"options": {"x": {"blob_ref": BLOB1}}}}

        violations = _validate_blob_content_refs_sync(blob_get_metadata=get_metadata, config=config)

        assert violations == [
            BlobInlineValidationViolation(
                category="malformed",
                field_path="source.options.x",
                detail="missing mode",
            )
        ]
        assert looked_up == []

    def test_sync_returns_malformed_violation_for_non_string_marker_fields(self) -> None:
        looked_up: list[UUID] = []

        def get_metadata(blob_id: UUID) -> BlobRecord | None:
            looked_up.append(blob_id)
            return None

        config = {"source": {"options": {"x": {"blob_ref": BLOB1, "mode": []}}}}

        violations = _validate_blob_content_refs_sync(blob_get_metadata=get_metadata, config=config)

        assert violations == [
            BlobInlineValidationViolation(
                category="malformed",
                field_path="source.options.x",
                detail="mode must be string",
            )
        ]
        assert looked_up == []

    def test_sync_reports_hash_mismatch_before_size(self) -> None:
        # A hash mismatch is the only violation reported for that ref even
        # when the record is also over the per-ref cap: the size comparison
        # is meaningless against bytes that are not the declared bytes.
        record = _blob_record(content_hash="b" * 64, size_bytes=128)
        config = {"source": {"options": {"x": _marker(BLOB1, VALID_HASH)}}}

        violations = _validate_blob_content_refs_sync(
            blob_get_metadata=lambda blob_id: record if blob_id == UUID(BLOB1) else None,
            config=config,
            per_ref_byte_cap=64,
        )

        assert [violation.category for violation in violations] == ["hash_mismatch"]
        assert violations[0].field_path == "source.options.x"

    def test_sync_returns_missing_violation_without_raising(self) -> None:
        config = {"source": {"options": {"x": _marker(BLOB1, VALID_HASH)}}}

        violations = _validate_blob_content_refs_sync(
            blob_get_metadata=lambda _blob_id: None,
            config=config,
        )

        assert violations == [
            BlobInlineValidationViolation(
                category="missing",
                field_path="source.options.x",
                detail=f"blob {UUID(BLOB1)} not found",
            )
        ]

    def test_sync_returns_not_ready_and_oversized_violations(self) -> None:
        pending = _blob_record(status="pending")
        too_large = _blob_record(blob_id=BLOB2, content_hash=VALID_HASH, size_bytes=128)
        config = {
            "source": {"options": {"x": _marker(BLOB1, VALID_HASH)}},
            "sinks": {"writeback": {"options": {"footer": _marker(BLOB2, VALID_HASH)}}},
        }

        def get_metadata(blob_id: UUID) -> BlobRecord | None:
            if blob_id == UUID(BLOB1):
                return pending
            if blob_id == UUID(BLOB2):
                return too_large
            return None

        violations = _validate_blob_content_refs_sync(
            blob_get_metadata=get_metadata,
            config=config,
            per_ref_byte_cap=64,
            aggregate_byte_cap=64,
        )

        assert [violation.category for violation in violations] == ["not_ready", "oversized", "oversized"]
        assert [violation.field_path for violation in violations] == [
            "source.options.x",
            "output:writeback.options.footer",
            "(aggregate)",
        ]


# --------------------------------------------------------------------------- #
# Emitted-section coverage (elspeth-ca79b2c63a)                                #
#                                                                              #
# `_NODE_COLLECTION_KEYS` was a hand-written tuple and omitted `collectors`,   #
# so a blob referenced only from a collector's options was never discovered    #
# and neither the per-ref byte cap nor the hash check ran on it.               #
#                                                                              #
# Nothing below lists a section name. The sections are DERIVED by asking the   #
# emitter what it produces, so a new options-bearing node kind is covered on   #
# the day it is emitted rather than the day someone remembers this file.       #
# --------------------------------------------------------------------------- #


def _pipeline_dict_with_every_node_kind() -> dict[str, Any]:
    """Emit a pipeline dict carrying one node of every declared kind.

    Built through `generate_pipeline_dict` on purpose: it is the sole producer
    of the shape these walks consume (`pipeline_dict_from_record` is
    `generate_pipeline_dict(state_from_record(record))`), so what it emits is
    the authority for what they must walk.
    """
    from elspeth.web.composer.state import COMPOSER_NODE_TYPES, CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
    from elspeth.web.composer.yaml_generator import generate_pipeline_dict

    def node(**overrides: Any) -> NodeSpec:
        base: dict[str, Any] = {
            "id": "n",
            "node_type": "transform",
            "input": "rows",
            "plugin": "p",
            "on_success": "out",
            "on_error": "discard",
            "options": {"payload": _marker()},
            "condition": None,
            "routes": None,
            "fork_to": None,
            "branches": None,
            "policy": None,
            "merge": None,
        }
        return NodeSpec(**{**base, **overrides})

    state = CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="rows",
            options={"payload": _marker()},
            on_validation_failure=None,
            description=None,
        ),
        nodes=(
            node(id="t", node_type="transform"),
            node(id="g", node_type="gate", plugin=None, options={}, condition="x", routes={"true": "out"}),
            node(id="a", node_type="aggregation"),
            node(id="c", node_type="coalesce", plugin=None, options={}, branches=("b1", "b2"), policy="require_all", merge="union"),
            node(id="r", node_type="row_union", plugin=None, options={}, branches=("b1", "b2")),
            node(id="q", node_type="queue", plugin=None, input="q", on_success=None, on_error=None, options={}),
            node(
                id="k",
                node_type="collector",
                on_error=None,
                scope_name="s",
                scope_opener="t",
                scope_policy="require_all",
            ),
        ),
        edges=(),
        outputs=(OutputSpec(name="out", plugin="json", options={"payload": _marker()}, on_write_failure=None, description=None),),
        metadata=PipelineMetadata(),
        version=1,
    )
    represented = {node.node_type for node in state.nodes}
    assert represented == COMPOSER_NODE_TYPES, (
        f"the coverage fixture no longer represents every Composer node kind; missing {sorted(COMPOSER_NODE_TYPES - represented)}, "
        f"obsolete {sorted(represented - COMPOSER_NODE_TYPES)}"
    )
    return generate_pipeline_dict(state)


def _options_bearing_sections(pipeline: dict[str, Any]) -> dict[str, list[str]]:
    """Derive `{section: [entry names]}` for every emitted section with options.

    Deliberately keyed on `options` ALONE. Do not narrow this to sections that
    also carry `plugin` — a blob reference hides in the options mapping, and
    whether the entry also names a plugin is a fact about a different concern.

    Handles both emitted container shapes: dict sections (`sources`, `sinks`)
    are keyed by entry name, list sections carry `name` on each entry.
    """
    sections: dict[str, list[str]] = {}
    for key, value in pipeline.items():
        named: list[str] = []
        if isinstance(value, dict):
            named = [name for name, entry in value.items() if isinstance(entry, dict) and entry.get("options")]
        elif isinstance(value, list):
            named = [entry["name"] for entry in value if isinstance(entry, dict) and entry.get("options") and "name" in entry]
        if named:
            sections[key] = named
    return sections


def test_inline_discovery_covers_every_options_bearing_emitted_section() -> None:
    pipeline = _pipeline_dict_with_every_node_kind()
    sections = _options_bearing_sections(pipeline)
    assert sections, "the emitter produced no options-bearing section — the fixture is inert"

    refs = _discover_blob_content_refs(pipeline)
    discovered = {ref.field_path for ref in refs}

    missed = sorted(
        section
        for section, names in sections.items()
        if not any(any(f":{name}." in path or path.startswith(f"{name}.") for path in discovered) for name in names)
    )

    assert not missed, (
        f"the emitter produces options-bearing {missed}, but inline discovery never walks {'it' if len(missed) == 1 else 'them'}: "
        f"neither the per-ref byte cap nor the hash check runs on a blob referenced there. "
        f"Discovered only {sorted(discovered)}. Derive the walked keys from the emitter rather than listing them."
    )
