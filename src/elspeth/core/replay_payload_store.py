"""Content-addressed payload access bound to source-run audit evidence.

The caller derives ``source_refs`` from retained source-run row and node
payloads, not from incoming row values. This prevents a replay row from using
an arbitrary hash as authority to read an unrelated payload in the store.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection

from elspeth.contracts.enums import RunMode
from elspeth.contracts.payload_store import IntegrityError, PayloadStore

_HASH = re.compile(r"[0-9a-f]{64}\Z")


class SourceBoundPayloadStore:
    """Read only audited source blobs and write only new-run audit payloads."""

    def __init__(
        self,
        *,
        mode: RunMode,
        source_store: PayloadStore,
        current_store: PayloadStore,
        source_refs: Collection[str],
        output_refs: Collection[str] = (),
    ) -> None:
        if mode not in (RunMode.REPLAY, RunMode.VERIFY):
            raise ValueError("SourceBoundPayloadStore requires replay or verify mode")
        for ref in (*source_refs, *output_refs):
            if type(ref) is not str or _HASH.fullmatch(ref) is None:
                raise ValueError("Source-run payload reference is not a SHA-256 hash")
        self._mode = mode
        self._source_store = source_store
        self._current_store = current_store
        self._source_refs = frozenset(source_refs)
        self._output_refs = frozenset(output_refs)

    def retrieve(self, content_hash: str) -> bytes:
        if content_hash not in self._source_refs:
            raise IntegrityError("Payload reference is absent from source-run audit evidence")
        original = self._source_store.retrieve(content_hash)
        self._check_hash(content_hash, original)
        if self._mode is RunMode.REPLAY:
            return original
        current = self._current_store.retrieve(content_hash)
        self._check_hash(content_hash, current)
        if current != original:
            raise IntegrityError("Current payload bytes differ from source-run evidence")
        return current

    def restore_output(self, content_hash: str) -> str:
        """Copy an archived output to the new run's audit store after checking it."""
        if self._mode is not RunMode.REPLAY:
            raise ValueError("restore_output is replay-only")
        if content_hash not in self._output_refs:
            raise IntegrityError("Output reference is absent from source-run audit evidence")
        original = self._source_store.retrieve(content_hash)
        self._check_hash(content_hash, original)
        stored_ref = self._current_store.store(original)
        if stored_ref != content_hash:
            raise IntegrityError("New-run audit store returned a different output hash")
        return stored_ref

    def store(self, content: bytes) -> str:
        if self._mode is RunMode.REPLAY:
            raise IntegrityError("Replay cannot store a newly computed payload")
        return self._current_store.store(content)

    def exists(self, content_hash: str) -> bool:
        return content_hash in self._source_refs and self._source_store.exists(content_hash)

    def delete(self, content_hash: str) -> bool:
        raise IntegrityError("A replay/verify run cannot delete payload evidence")

    @staticmethod
    def _check_hash(content_hash: str, content: bytes) -> None:
        if hashlib.sha256(content).hexdigest() != content_hash:
            raise IntegrityError("Payload content does not match source-run hash")
