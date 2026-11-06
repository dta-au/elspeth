"""One-stage block hashes never retain mutable recorder trees or change refusal order."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_audit_buffer as buffer
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.continuation_retention_memo import (
    RecorderRequestTextKey,
    RecorderRequestTextProof,
    retention_stage_memo_scope,
)


def _proof() -> RecorderRequestTextProof:
    captured = capture_provider_context(
        messages=[{"role": "user", "content": "First inspection."}, {"role": "assistant", "content": "Second inspection."}],
        tools=None,
    )
    assert captured.disposition == "retained" and captured.canonical is not None
    proof = buffer._prepared_request_text(RecorderRequestTextKey(captured.canonical, CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES))
    assert proof is not None
    return proof


def test_block_hash_reuse_keeps_fresh_strict_parse_and_independent_mutable_values(monkeypatch):
    proof = _proof()
    actual_hash = buffer._require_recorder_block_hash
    actual_parse = buffer.parse_json_strict
    hashed = []
    parsed = []

    def observe_hash(key):
        hashed.append(key)
        return actual_hash(key)

    def observe_parse(canonical):
        parsed.append(canonical)
        return actual_parse(canonical)

    monkeypatch.setattr(buffer, "_require_recorder_block_hash", observe_hash)
    monkeypatch.setattr(buffer, "parse_json_strict", observe_parse)
    first_blocks, second_blocks = buffer._RecorderBlocks(), buffer._RecorderBlocks()
    with retention_stage_memo_scope():
        first = buffer._transfer_prepared_request_text(proof, first_blocks)
        first_document = first_blocks.document()
        assert type(first) is dict and type(first_document) is dict
        first_value = first_document[proof.blocks[0][0]]
        assert type(first_value) is dict
        first_value["content"] = "Changed after transfer."
        first["messages"][0] = "0" * 64
        second = buffer._transfer_prepared_request_text(replace(proof), second_blocks)
        second_document = second_blocks.document()
        assert type(second) is dict and type(second_document) is dict
        assert second["messages"][0] == proof.blocks[0][0]
        second_value = second_document[proof.blocks[0][0]]
        assert type(second_value) is dict
        assert second_value["content"] == "First inspection."
        assert second_value is not first_value
        assert len(hashed) == len(proof.blocks)
        for _key, canonical in proof.blocks:
            assert parsed.count(canonical) == 2


@pytest.mark.parametrize("change", ("canonical", "digest"))
def test_warm_block_hash_does_not_admit_changed_complete_binding(change):
    proof = _proof()
    original_key, original_text = proof.blocks[0]
    if change == "canonical":
        changed = replace(proof, blocks=((original_key, canonical_json({"role": "user", "content": "Substituted."})), *proof.blocks[1:]))
    else:
        foreign_key = "0" * 64
        references, error = buffer.parse_json_strict(proof.ref_document_json)
        assert error is None and type(references) is dict
        references["messages"][0] = foreign_key
        changed = replace(
            proof,
            ref_document_json=canonical_json(references),
            blocks=((foreign_key, original_text), *proof.blocks[1:]),
        )
    with retention_stage_memo_scope():
        buffer._transfer_prepared_request_text(proof, buffer._RecorderBlocks())
        with pytest.raises(AuditIntegrityError, match="content address"):
            buffer._transfer_prepared_request_text(changed, buffer._RecorderBlocks())


def test_block_hash_hit_still_preserves_first_block_parse_before_later_hash(monkeypatch):
    malformed = "not-json"
    first_key = hashlib.sha256(malformed.encode("utf-8")).hexdigest()
    later_key = "0" * 64
    proof = RecorderRequestTextProof(
        canonical_json(
            {
                "disposition": "retained",
                "schema_version": "composer.provider-context.v1",
                "capture_boundary": "prepared_litellm_messages_and_tools",
                "messages": [first_key, later_key],
                "tools": None,
            }
        ),
        ((first_key, malformed), (later_key, "null")),
    )
    actual_hash = buffer._require_recorder_block_hash
    hashed = []

    def observe_hash(key):
        hashed.append(key)
        return actual_hash(key)

    monkeypatch.setattr(buffer, "_require_recorder_block_hash", observe_hash)
    with retention_stage_memo_scope():
        for _ in range(2):
            blocks = buffer._RecorderBlocks()
            with pytest.raises(AuditIntegrityError, match="not strict JSON"):
                buffer._transfer_prepared_request_text(proof, blocks)
            assert blocks.document() == {}
    assert len(hashed) == 1 and hashed[0].canonical == malformed


def test_block_hash_producer_identity_and_stage_lifetime_do_not_share_proofs(monkeypatch):
    proof = _proof()
    actual_hash = buffer._require_recorder_block_hash
    first_calls, replacement_calls = [], []

    def first(key):
        first_calls.append(key)
        return actual_hash(key)

    def replacement(key):
        replacement_calls.append(key)
        return actual_hash(key)

    monkeypatch.setattr(buffer, "_require_recorder_block_hash", first)
    with retention_stage_memo_scope():
        buffer._transfer_prepared_request_text(proof, buffer._RecorderBlocks())
        monkeypatch.setattr(buffer, "_require_recorder_block_hash", replacement)
        buffer._transfer_prepared_request_text(proof, buffer._RecorderBlocks())
        buffer._transfer_prepared_request_text(proof, buffer._RecorderBlocks())
    with retention_stage_memo_scope():
        buffer._transfer_prepared_request_text(proof, buffer._RecorderBlocks())
    assert len(first_calls) == len(proof.blocks)
    assert len(replacement_calls) == 2 * len(proof.blocks)


def test_block_hash_failure_remains_original_and_is_not_cached(monkeypatch):
    proof = _proof()
    original = RuntimeError("original block proof failure")
    calls = []

    def fail(key):
        calls.append(key)
        raise original

    monkeypatch.setattr(buffer, "_require_recorder_block_hash", fail)
    with retention_stage_memo_scope():
        for _ in range(2):
            blocks = buffer._RecorderBlocks()
            with pytest.raises(RuntimeError) as caught:
                buffer._transfer_prepared_request_text(proof, blocks)
            assert caught.value is original and blocks.document() == {}
    assert len(calls) == 2


@pytest.mark.parametrize("replacement", ("valid", "different", "raise"))
def test_warm_block_proof_cannot_hide_replaced_crypto_producer(monkeypatch, replacement):
    proof = _proof()
    original_hash = buffer.hashlib.sha256
    original_error = RuntimeError("original current crypto failure")
    calls = []

    def current_hash(encoded):
        calls.append(encoded)
        if replacement == "raise":
            raise original_error
        return original_hash(encoded if replacement == "valid" else b"substituted block")

    with retention_stage_memo_scope():
        original_blocks = buffer._RecorderBlocks()
        original = buffer._transfer_prepared_request_text(proof, original_blocks)
        monkeypatch.setattr(buffer.hashlib, "sha256", current_hash)
        if replacement == "valid":
            for _ in range(2):
                blocks = buffer._RecorderBlocks()
                assert buffer._transfer_prepared_request_text(proof, blocks) == original
                assert blocks.document() == original_blocks.document()
            assert len(calls) == 2 * len(proof.blocks)
        elif replacement == "different":
            with pytest.raises(AuditIntegrityError, match="content address"):
                buffer._transfer_prepared_request_text(proof, buffer._RecorderBlocks())
            assert len(calls) == 1
        else:
            with pytest.raises(RuntimeError) as caught:
                buffer._transfer_prepared_request_text(proof, buffer._RecorderBlocks())
            assert caught.value is original_error and len(calls) == 1
