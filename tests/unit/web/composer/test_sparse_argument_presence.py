"""Authored presence stays exact while the no-inline-blob binding is semantic."""

import json
from copy import deepcopy
from types import MappingProxyType

import pytest

from elspeth.web.composer.audit import begin_dispatch, finish_success
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from elspeth.web.composer.authority_hashing import composer_authority_hash
from elspeth.web.composer.pipeline_commit import PipelineDispatchAuditBinding
from elspeth.web.composer.redaction import redact_tool_call_arguments, semantic_redacted_pipeline_arguments_hash
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from tests.unit.web.composer.test_row_union_authority_hashing import _dispatch_result


def _redact(arguments):
    return redact_tool_call_arguments(
        "set_pipeline", {"nodes": [], "edges": [], "outputs": [], **arguments}, telemetry=NoopRedactionTelemetry()
    )


def test_sparse_pipeline_retains_explicit_null_and_sensitive_presence() -> None:
    arguments = {"source": {"plugin": "csv", "on_success": "rows", "options": {"path": "PRIVATE_PATH"}}}
    original = deepcopy(arguments)
    omitted = _redact(arguments)
    explicit = _redact({"source": {**arguments["source"], "inline_blob": None}})
    assert set(omitted) == {"source", "nodes", "edges", "outputs"}
    assert set(omitted["source"]) == {"plugin", "on_success", "options"}
    assert explicit["source"]["inline_blob"] is None
    assert "PRIVATE_PATH" not in str(omitted)
    assert arguments == original
    assert composer_authority_hash(omitted) != composer_authority_hash(explicit)
    assert semantic_redacted_pipeline_arguments_hash(omitted) == semantic_redacted_pipeline_arguments_hash(explicit)


@pytest.mark.parametrize("field,value", [("description", None), ("on_success", "different"), ("options", "different summary")])
def test_semantic_hash_does_not_normalize_other_source_fields(field, value) -> None:
    arguments = {"source": {"plugin": "csv"}}
    changed = {"source": {"plugin": "csv", field: value}}
    assert semantic_redacted_pipeline_arguments_hash(arguments) != semantic_redacted_pipeline_arguments_hash(changed)


def test_semantic_hash_keeps_named_source_null_and_preserves_frozen_input() -> None:
    omitted = {"sources": {"named": {"plugin": "csv"}}}
    explicit = {"sources": {"named": {"plugin": "csv", "inline_blob": None}}}
    assert semantic_redacted_pipeline_arguments_hash(omitted) != semantic_redacted_pipeline_arguments_hash(explicit)
    frozen = MappingProxyType({"source": MappingProxyType({"plugin": "csv", "inline_blob": None})})
    assert semantic_redacted_pipeline_arguments_hash(frozen) == semantic_redacted_pipeline_arguments_hash({"source": {"plugin": "csv"}})
    assert "inline_blob" in frozen["source"]


def test_sparse_sensitive_metadata_summaries_reflect_authored_keys() -> None:
    source = {"source": {"plugin": "csv", "on_success": "rows", "options": {}}}
    assert "metadata" not in _redact(source)
    empty = _redact({**source, "metadata": {}})
    named = _redact({**source, "metadata": {"name": "PRIVATE_NAME"}})
    assert empty["metadata"] != named["metadata"]
    assert "PRIVATE_NAME" not in str(named)


def test_actual_audit_projection_preserves_null_while_dispatch_binding_agrees() -> None:
    omitted = {"source": {"plugin": "csv", "on_success": "rows", "options": {}}, "nodes": [], "edges": [], "outputs": []}
    explicit = deepcopy(omitted)
    explicit["source"]["inline_blob"] = None
    bindings = []
    private_hashes = []
    for arguments in (omitted, explicit):
        invocation = finish_success(
            begin_dispatch("same-call", "set_pipeline", arguments, version_before=1, actor="test"),
            result_payload=_dispatch_result(),
            version_after=2,
        )
        private_hashes.append(invocation.arguments_hash)
        _content, envelope = redacted_tool_invocation_content_and_envelope(invocation)
        displayed = json.loads(envelope["invocation"]["arguments_canonical"])
        assert ("inline_blob" in displayed["source"]) == (arguments is explicit)
        bindings.append(PipelineDispatchAuditBinding.from_persisted_envelope(envelope))
    assert bindings[0] == bindings[1]
    assert private_hashes[0] != private_hashes[1]
