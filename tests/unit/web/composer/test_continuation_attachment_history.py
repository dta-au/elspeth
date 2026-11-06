from dataclasses import replace
from hashlib import sha256

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.attachment_admission import AttachmentInspectionPolicy, AttachmentInspectionRefused, inspect_attachment_original
from elspeth.web.composer.continuation_attachment_application_values import AttachmentApplicationData
from elspeth.web.composer.continuation_attachment_history import (
    append_attachment_inspections,
    decode_attachment_history,
    empty_attachment_history,
    encode_attachment_history,
    extend_attachment_history,
)
from elspeth.web.credential_guard import CredentialMaterialRefused


def inspect(raw=b"complete original", *, policy=None):
    return inspect_attachment_original(
        session_id="session",
        blob_id="blob",
        verified_hash=sha256(raw).hexdigest(),
        raw=raw,
        policy=policy or AttachmentInspectionPolicy(100000),
    )


def test_actual_inspections_preserve_multiplicity_history_and_idempotent_effect():
    receipt = inspect()
    history = empty_attachment_history(session_id="session", operation_id="logical-operation")
    first = append_attachment_inspections(history, effect_key="first", inspections=(receipt, receipt))
    second = append_attachment_inspections(first, effect_key="second", inspections=(inspect(),))
    assert len(second.reads) == 2
    assert len(second.reads[0].inspections) == 2
    assert second.reads[0].inspections[0].detector_version == receipt.detector_version
    assert second.reads[0].inspections[0].policy_fingerprint == receipt.policy_fingerprint
    assert append_attachment_inspections(second, effect_key="first", inspections=(receipt, receipt)) is second
    restored = decode_attachment_history(encode_attachment_history(second), session_id="session", operation_id="logical-operation")
    assert restored == second
    # History does not use today's policy fingerprint as clearance.
    changed = inspect(policy=AttachmentInspectionPolicy(99999))
    assert changed.policy_fingerprint != receipt.policy_fingerprint
    assert decode_attachment_history(encode_attachment_history(second), session_id="session", operation_id="logical-operation") == second
    with pytest.raises(AuditIntegrityError):
        append_attachment_inspections(second, effect_key="first", inspections=(changed,))
    with pytest.raises(AuditIntegrityError):
        decode_attachment_history(encode_attachment_history(second), session_id="other", operation_id="logical-operation")


def test_complete_original_bound_hash_utf8_and_late_credential_refusals():
    clean = ("é" * 35000).encode()
    assert inspect(clean).size_bytes == len(clean)
    with pytest.raises(CredentialMaterialRefused):
        inspect(clean + b" https://name:private-value@example.test/")
    with pytest.raises(AttachmentInspectionRefused):
        inspect(clean, policy=AttachmentInspectionPolicy(len(clean) - 1))
    with pytest.raises(UnicodeDecodeError):
        inspect(b"\xff")
    with pytest.raises(AuditIntegrityError):
        inspect_attachment_original(
            session_id="session", blob_id="blob", verified_hash="0" * 64, raw=clean, policy=AttachmentInspectionPolicy(100000)
        )


def test_actual_receipt_wrong_session_and_impostor_refuse():
    actual = inspect()
    history = empty_attachment_history(session_id="session", operation_id="logical-operation")
    assert append_attachment_inspections(history, effect_key="first", inspections=(actual,)).reads
    with pytest.raises(ValueError):
        append_attachment_inspections(history, effect_key="first", inspections=(replace(actual, session_id="other"),))
    with pytest.raises(TypeError):
        append_attachment_inspections(history, effect_key="first", inspections=({},))


def _application(effect_key):
    return AttachmentApplicationData(
        effect_key=effect_key,
        source_checkpoint_id="checkpoint",
        source_cursor=0,
        source_snapshot_json="{}",
        prepared_json="{}",
        response_json="{}",
        published_result_json="{}",
        receipt_json="{}",
        before_evidence_json="{}",
        after_evidence_json="{}",
        after_batch_json="{}",
        before_iteration_json="{}",
        after_iteration_json="{}",
        before_caches_json="{}",
        after_caches_json="{}",
        application_sequence=1,
        application_generation=1,
        application_operation_id="operation",
        application_operation_epoch=1,
    )


def test_history_extension_refuses_a_repeated_application():
    """The application append (sessions/continuation_attachment_application.py)
    extends through this builder. Control: build with model_copy -> red."""
    history = extend_attachment_history(
        empty_attachment_history(session_id="session", operation_id="operation"), applications=(_application("apply:one"),)
    )
    with pytest.raises(ValueError, match="repeats an actual application"):
        extend_attachment_history(history, applications=(_application("apply:one"),))


def test_historical_effect_identifier_keeps_string_credential_guard():
    from elspeth.web.composer.continuation_attachment_history import ContinuationAttachmentHistory

    normal = append_attachment_inspections(
        empty_attachment_history(session_id="session", operation_id="operation"), effect_key="read:one", inspections=(inspect(),)
    )
    assert decode_attachment_history(encode_attachment_history(normal), session_id="session", operation_id="operation") == normal
    wire = normal.model_dump(mode="python")
    read = normal.reads[0].model_dump(mode="python")
    read["effect_key"] = "sk-" + "a" * 24
    wire["reads"] = (read,)
    changed = ContinuationAttachmentHistory.model_validate(wire)
    with pytest.raises(CredentialMaterialRefused):
        encode_attachment_history(changed)


def test_actual_declared_json_slot_and_environment_policy_take_effect():
    policy = AttachmentInspectionPolicy(10000, env_ref_names=frozenset({"DB_URL"}), additional_credential_fields=frozenset({"opaque_slot"}))
    clean = b'{"label":"customer_key","opaque_slot":"${DB_URL}"}'
    actual = inspect(clean, policy=policy)
    assert actual.content_hash == sha256(clean).hexdigest()
    assert actual.size_bytes == len(clean)
    assert actual.policy_fingerprint == policy.fingerprint
    with pytest.raises(CredentialMaterialRefused):
        inspect(b'{"opaque_slot":"opaque-private-value"}', policy=policy)
    with pytest.raises(CredentialMaterialRefused):
        inspect(b'{"opaque_slot":"${UNKNOWN}"}', policy=policy)
    with pytest.raises(CredentialMaterialRefused):
        inspect(b'{"opaque\\u005fslot":"opaque-private-value"}', policy=policy)
    # Additional field names apply to JSON object slots, not arbitrary prose.
    assert inspect(b"ordinary customer_key prose", policy=policy).size_bytes == 27


@pytest.mark.parametrize("raw", (b'{"opaque_slot":null,"opaque_slot":"x"}', b'{"value":NaN}', b'{"unfinished":', b'"unfinished'))
def test_json_shaped_original_refuses_lossy_or_malformed_parse(raw):
    assert inspect(b'{"value":null}').size_bytes == 14
    with pytest.raises(AttachmentInspectionRefused):
        inspect(raw)


def test_json_escaped_credential_text_is_scanned_after_decode():
    assert inspect(b'"ordinary text"').size_bytes == 15
    escaped = b'"s\\u006b-' + b"a" * 24 + b'"'
    with pytest.raises(CredentialMaterialRefused):
        inspect(escaped)


@pytest.mark.parametrize("raw", (b'{"value":1e999}', b'{"value":[-1e999]}', b"1e999"))
def test_json_overflow_is_refused_after_finite_positive(raw):
    finite = b'{"value":1e300,"integer":9007199254740993}'
    assert inspect(finite).content_hash == sha256(finite).hexdigest()
    with pytest.raises(AttachmentInspectionRefused, match="nonfinite") as caught:
        inspect(raw)
    assert type(caught.value.__cause__) is ValueError
