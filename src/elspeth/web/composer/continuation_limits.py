"""Continuation storage admission policies, independent of untrusted input JSON.

These unchanged per-document and two-document allowances do not establish a
complete operation, root snapshot, memory, producer-output or work budget.
"""

CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES = 1_048_576
CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES = 1_048_576
