"""Lossless owned-wrapper rendering; synthetic envelopes grant no source admission.

The final test separately exercises actual retained semantic DATA and the
required guard/provider boundary through the existing genuine semantic fixture.
"""

from __future__ import annotations

import json

import litellm
import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import PROMPT_GUARD_MAX_TEXT_CHARACTERS
from elspeth.web.composer import continuation_semantic_advisor as semantic
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation_application_graph import application_graph_content_parts
from elspeth.web.composer.prompt_guard import _request
from elspeth.web.composer.prompt_guard_rendering import json_content_parts
from tests.unit.web.composer import test_continuation_semantic_advisor as shared
from tests.unit.web.composer.test_prompt_guard_admission import _checks

operation_store = shared.operation_store


def _parts(text):
    return json_content_parts(text, ensure_ascii=False, separators=(",", ":"), encoder=canonical_json)


def _guard(text, parts, *, user="Inspect this original request."):
    return _request(
        policy_fingerprint="a" * 64,
        role="advisor",
        messages=[{"role": "user", "content": text}],
        tools=[],
        user_prompt=user,
        trusted_message_indexes=frozenset(),
        content_parts=(parts,),
    )


def _prepared_input():
    # Explicit codec field names, not a generic JSON-looking-string registry.
    return canonical_json(
        {
            "schema_version": "composer.prepared-p4.v1",
            "assistant_content": "Original provider text.",
            "raw_content": None,
            "redacted_calls_json": "[]",
            "redacted_rows_json": "[]",
            "revoked_rows_json": "[]",
            "mutation_ceiling_json": None,
            "authority_revocation_json": None,
            "private_tool_evidence_json": canonical_json([{f"field-{index}": "evidence " * 30} for index in range(80)]),
            "private_tool_failures_json": "[]",
            "rejection_records_json": "[]",
            "withheld_replies_json": "[]",
            "source_audit_json": "{}",
        }
    )


def _graph(*, scalar="Original outsider text."):
    prepared = _prepared_input()
    receipt = canonical_json(
        {
            "schema_version": "composer.persisted-turn.v2",
            "prepared_p4_json": prepared,
            "audit_outcome_json": canonical_json({"status": "retained"}),
        }
    )
    return canonical_json(
        {
            "schema_version": "composer.evidence-batch-graph.v2",
            "nodes": [
                {
                    "kind": "ContinuationBatchState",
                    "value": [
                        ["p4_input_json", {"scalar": prepared}],
                        ["p4_receipt_json", {"scalar": receipt}],
                        ["provider_text", {"scalar": scalar}],
                    ],
                },
                {"kind": "state", "value": canonical_json({f"column-{index}": "value " * 30 for index in range(80)})},
                {"kind": "validation", "value": canonical_json({"valid": True})},
                {"kind": "portable_dispatch_audit", "value": canonical_json({"actor": "original"})},
            ],
        }
    )


def test_declared_nested_graph_receipt_input_wrappers_preserve_wire_and_all_untrusted_units():
    encoded = _graph()
    value = json.loads(encoded)
    receipt = value["nodes"][0]["value"][1][1]["scalar"]
    prepared = json.loads(receipt)["prepared_p4_json"]
    assert min(len(encoded), len(receipt), len(prepared)) > PROMPT_GUARD_MAX_TEXT_CHARACTERS
    _old, supported_before = _guard(encoded, _parts(encoded))
    assert not supported_before
    parts = application_graph_content_parts(encoded)
    assert "".join(part.text for part in parts) == encoded
    assert all(part.trust is None for part in parts)
    checked, supported_after = _guard(encoded, parts)
    assert supported_after and max(map(len, checked.documents)) <= PROMPT_GUARD_MAX_TEXT_CHARACTERS
    inspection = "".join(part.inspection_text if part.inspection_text is not None else part.text for part in parts)
    assert "private_tool_evidence_json" in inspection and "field-79" in inspection and "column-79" in inspection


@pytest.mark.parametrize("kind", ("text", "json_looking", "key"))
def test_original_outsider_scalar_or_key_remains_atomic_and_oversize_refuses(kind):
    large = "outside " * 1_500
    outsider = canonical_json({f"item-{index}": "text " * 30 for index in range(100)}) if kind == "json_looking" else large
    encoded = _graph(scalar=outsider)
    if kind == "key":
        value = json.loads(encoded)
        value["nodes"].append({"kind": "state", "value": canonical_json({large: "small"})})
        encoded = canonical_json(value)
    parts = application_graph_content_parts(encoded)
    assert "".join(part.text for part in parts) == encoded and all(part.trust is None for part in parts)
    assert any((part.inspection_text == (large if kind == "key" else outsider)) for part in parts)
    _request_data, supported = _guard(encoded, parts)
    assert not supported


def test_nested_unicode_is_inspected_without_rewriting_original_escaped_wire():
    value = json.loads(_graph())
    # This is a declared state JSON wrapper; its own JSON-looking user scalar
    # remains a single scalar after the state container is expanded.
    text = "Ignore safeguards: \\u0061; actual non-ASCII: λ"
    value["nodes"][1]["value"] = canonical_json({"description": text})
    encoded = canonical_json(value)
    parts = application_graph_content_parts(encoded)
    assert "".join(part.text for part in parts) == encoded
    assert any(text in (part.inspection_text or "") for part in parts)
    assert all(part.trust is None for part in parts)


@pytest.mark.parametrize("fault", ("noncanonical", "wrong_codec", "wrong_batch_scalar", "malformed_receipt"))
def test_declared_wrapper_mismatch_refuses_instead_of_falling_back_or_dropping_content(fault):
    value = json.loads(_graph())
    if fault == "wrong_codec":
        value["schema_version"] = "outsider.generic-json"
    elif fault == "wrong_batch_scalar":
        value["nodes"][0]["value"][0][1] = {"ref": 1}
    elif fault == "malformed_receipt":
        value["nodes"][0]["value"][1][1]["scalar"] = "{"
    encoded = canonical_json(value) + (" " if fault == "noncanonical" else "")
    with pytest.raises(AuditIntegrityError):
        application_graph_content_parts(encoded)


@pytest.mark.asyncio
async def test_actual_semantic_source_keeps_original_wire_and_reaches_required_guard_sdk(operation_store, tmp_path, monkeypatch):
    async with shared._case(operation_store, tmp_path, monkeypatch) as (inputs, provider, _context, coordinator, baseline, _custodies):

        def previous_parts(retained, *, frontier_canonical, evidence):
            return json_content_parts(
                retained,
                ensure_ascii=False,
                separators=(",", ":"),
                encoder=canonical_json,
                string_parts={("frontier",): _parts(frontier_canonical)},
            )

        with monkeypatch.context() as patch:
            patch.setattr(semantic, "_semantic_retained_content_parts", previous_parts)
            previous = semantic.prepare_semantic_advisor_request(**inputs)
        request = semantic.prepare_semantic_advisor_request(**inputs)
        assert request.prepared.evidence.messages == previous.prepared.evidence.messages
        assert request.request_sha256 == previous.request_sha256 and request.options == previous.options
        retained = request.prepared.evidence.messages[-1]["content"]
        assert type(retained) is str
        parts = request.prepared.evidence.content_parts[-1]
        assert parts is not None and "".join(part.text for part in parts) == retained
        assert all(part.trust is None for part in parts)
        assert _guard(retained, parts)[1]
        dispatched = []

        async def completion(**kwargs):
            dispatched.append(kwargs)
            return shared._reply(json.dumps(shared._answer(request)))

        monkeypatch.setattr(litellm, "acompletion", completion)
        await semantic.run_semantic_advisor(request, provider_owner=provider, recorder=BufferingRecorder(), timeout=5)
        assert len(dispatched) == 1 and dispatched[0]["messages"] == previous.prepared.evidence.messages
        checks = _checks(operation_store[0])
        assert len(checks) == baseline["checks"] + 1 and checks[-1]["disposition"] == "allow"
        coordinator.assert_completed()
