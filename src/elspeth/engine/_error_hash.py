"""Shared, empty-safe computation of the 16-char audit ``error_hash``.

``error_hash`` is a content fingerprint of a row's originating error, used for
per-row audit attribution. Hashing an empty message collapses every
empty-message error into the constant ``sha256("")`` prefix
(``e3b0c44298fc1c14``), so distinct empty-message failures become
indistinguishable in the audit trail — defeating the attributability guarantee
(elspeth-501c14847b). An empty message is a legitimate input here
(``str(ValueError()) == ""``), so the empty branch hashes a type-qualified,
domain-separated byte sequence rather than rejecting it on the
failure-recording path. The leading ``0xff`` cannot occur in an ordinary
UTF-8-encoded message, preventing a message from spoofing the empty branch
(elspeth-dcb54f8b1a).

Crucially this changes ONLY the empty case: for any non-empty message the result
is byte-identical to the previous inline ``sha256(msg.encode()).hexdigest()[:16]``,
so no existing audit hash changes and no fingerprint-baseline reconciliation is
needed.
"""

from __future__ import annotations

import hashlib

_EMPTY_MESSAGE_DOMAIN = b"\xffelspeth:error_hash:no-message\x00"


def compute_error_hash(message: str, *, exception_type: str | None = None) -> str:
    """Return the 16-char sha256 prefix of an error message, empty-safe.

    For a non-empty ``message`` the result equals
    ``sha256(message.encode()).hexdigest()[:16]`` exactly. For an empty
    ``message`` a domain-separated byte sequence is hashed instead — qualified by
    ``exception_type`` when available — so empty-message errors remain
    distinguishable by type and cannot collide with an ordinary UTF-8 message.
    """
    if not message:
        payload = _EMPTY_MESSAGE_DOMAIN + (exception_type or "").encode()
    else:
        payload = message.encode()
    return hashlib.sha256(payload).hexdigest()[:16]
