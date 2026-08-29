"""Prepare and verify guided JSON payloads outside SQL transactions."""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from types import MappingProxyType

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_freeze
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.payload_store import PayloadStore
from elspeth.web.sessions.protocol import GuidedJsonPayloadPurpose, PreparedGuidedJsonPayload


def _guided_payload_envelope(
    *,
    purpose: GuidedJsonPayloadPurpose,
    payload: Mapping[str, object],
) -> Mapping[str, object]:
    return {
        "schema": "guided.json-payload.v1",
        "purpose": purpose,
        "payload": payload,
    }


def prepare_guided_json_payload(
    payload_store: PayloadStore,
    *,
    purpose: GuidedJsonPayloadPurpose,
    payload: Mapping[str, object],
) -> PreparedGuidedJsonPayload:
    """Freeze, store, retrieve, and byte-verify one content-addressed payload.

    ``payload_store`` carries no runtime type gate. ``PayloadStore`` is a
    ``runtime_checkable`` Protocol, which ADR-032 withdraws as a control: it
    tests only for four method names, so an impostor passes and a
    dynamic-attribute implementation is rejected — it proves nothing beyond
    what the nominal annotation already states. The custody control is the
    store/retrieve/``hmac.compare_digest`` round trip below, which no
    substituted store satisfies without durably holding the exact bytes.
    """

    snapshot = deep_freeze(payload)
    if type(snapshot) is not MappingProxyType:
        raise TypeError("payload must freeze to an immutable mapping")
    canonical = canonical_json(_guided_payload_envelope(purpose=purpose, payload=snapshot)).encode("utf-8")
    payload_id = payload_store.store(canonical)
    if type(payload_id) is not str or len(payload_id) != 64:
        raise AuditIntegrityError("guided payload store returned a malformed content id")
    retrieved = payload_store.retrieve(payload_id)
    if not hmac.compare_digest(retrieved, canonical):
        raise AuditIntegrityError("guided payload store retrieval differs from the stored canonical bytes")
    return PreparedGuidedJsonPayload(payload_id=payload_id, purpose=purpose, payload=snapshot)


def verify_guided_json_payloads(
    payload_store: PayloadStore | None,
    payloads: tuple[PreparedGuidedJsonPayload, ...],
) -> None:
    """Re-read each referenced payload immediately before SQL settlement.

    Only the "configured at all" half of the old admission test survives: the
    ``runtime_checkable`` Protocol ``isinstance`` beside it admitted any
    four-method impostor (ADR-032) while the re-read plus
    ``hmac.compare_digest`` below is what actually proves custody.
    """

    if not payloads:
        return
    if payload_store is None:
        raise AuditIntegrityError("guided payload settlement requires a configured PayloadStore")
    for payload in payloads:
        expected = canonical_json(
            _guided_payload_envelope(
                purpose=payload.purpose,
                payload=payload.payload,
            )
        ).encode("utf-8")
        retrieved = payload_store.retrieve(payload.payload_id)
        if not hmac.compare_digest(retrieved, expected):
            raise AuditIntegrityError("guided payload store content differs from the prepared payload")


__all__ = ["prepare_guided_json_payload", "verify_guided_json_payloads"]
