"""Continuation storage admission policies, independent of untrusted input JSON.

These unchanged per-document and two-document allowances do not establish a
complete operation, root snapshot, memory, producer-output or work budget.
"""

from elspeth.contracts.errors import AuditIntegrityError


class ContinuationDocumentTooLarge(AuditIntegrityError):
    """A complete generated document cannot fit the unchanged admission limit."""


CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES = 1_048_576
CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES = 1_048_576
# An action-local RAM cap for immutable Advisor text proofs, not an admission
# limit for an individual document or evidence pair.
CONTINUATION_LEGACY_ADVISOR_TEXT_MEMO_MAX_RETAINED_BYTES = 5_242_880
# Transient V4 preparation text has its own memory budget, independent of
# private-document admission and each individual retained proof's bound.
CONTINUATION_V4_PREPARATION_TEXT_MEMO_MAX_RETAINED_BYTES = 5_242_880
