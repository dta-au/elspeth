"""Metadata-only structured event logging.

``log_event`` is the gateway's only logging entry point for anything derived
from a request or an upstream exchange: it accepts fields exclusively by
keyword and raises ``ValueError`` on any field name outside ``SAFE_FIELDS``.
There is no escape hatch (no ``**extra``, no free-text ``message``) — a
caller that wants to log something new must add it to the allowlist here
first, which keeps prompt text, bearer tokens, client secrets, and upstream
response bodies from ever reaching a log record by construction rather than
by review.

``canonical_hash`` gives call sites a way to record *that* a particular
request or response occurred, and to compare two occurrences for equality,
without ever logging the content itself.
"""

import hashlib
import json
import logging
from typing import Any

SAFE_FIELDS: frozenset[str] = frozenset(
    {
        "request_id",
        "request_hash",
        "response_hash",
        "contract_major",
        "adapter_name",
        "adapter_version",
        "adapter_api_major",
        "adapter_fingerprint",
        "model_alias",
        "mapping_generation",
        "status",
        "latency_ms",
        "response_bytes",
        "upstream_status_class",
        "oauth_cache_hit",
        "oauth_refresh",
        "oauth_refresh_outcome",
        "error_code",
        "event",
    }
)


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Log ``event`` plus ``fields`` at INFO, or raise if any field is unsafe.

    Every key in ``fields`` must be a member of ``SAFE_FIELDS`` — any other
    key raises ``ValueError`` before anything is logged, so a call site
    cannot smuggle an unreviewed field (e.g. a raw prompt or upstream body)
    into the log stream just by naming it. The record is logged as a single
    compact JSON object so nothing structured gets lost to a formatter.
    """
    unsafe = sorted(set(fields) - SAFE_FIELDS)
    if unsafe:
        raise ValueError(f"unsafe event fields: {unsafe}")

    record: dict[str, Any] = {"event": event, **fields}
    logger.info(json.dumps(record, sort_keys=True, separators=(",", ":")))


def canonical_hash(obj: Any) -> str:
    """A stable, order-independent fingerprint of ``obj``.

    Serialises ``obj`` as compact JSON with sorted keys before hashing, so
    two structurally-equal objects that differ only in key order or
    incidental whitespace hash identically. Returns the first 32 hex
    characters of the SHA-256 digest — enough to detect a change or compare
    two occurrences, without logging the content itself.
    """
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
