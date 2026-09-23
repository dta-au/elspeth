"""Content-addressed payload access bound to source-run audit evidence.

The caller derives ``source_refs`` from retained source-run row and node
payloads, not from incoming row values. This prevents a replay row from using
an arbitrary hash as authority to read an unrelated payload in the store.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from elspeth.contracts.audit import NodeStateCompleted
from elspeth.contracts.enums import CallStatus, CallType, RunMode
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.payload_store import IntegrityError, PayloadStore
from elspeth.core.landscape.row_data import CallDataState, RowDataState

if TYPE_CHECKING:
    from elspeth.core.landscape.factory import LandscapeReadRepositories, RecorderFactory

_HASH = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class SourcePayloadRefs:
    """Refs proven by retained source-run row, token and call evidence."""

    input_refs: frozenset[str]
    output_refs: frozenset[str]


def _audited_ref(value: object, *, location: str) -> str:
    if type(value) is not str or _HASH.fullmatch(value) is None:
        raise AuditIntegrityError(f"{location}: expected a SHA-256 payload reference")
    return value


def _row_refs(row: object, *, fields: frozenset[str], location: str) -> Iterable[str]:
    if not isinstance(row, Mapping):
        raise AuditIntegrityError(f"{location}: audited row is not an object")
    for field_name in fields:
        if field_name in row:
            yield _audited_ref(row[field_name], location=f"{location}.{field_name}")


def collect_source_payload_refs(
    factory: RecorderFactory | LandscapeReadRepositories,
    source_run_id: str,
    *,
    source_store: PayloadStore,
    blob_ref_fields: Collection[str],
) -> SourcePayloadRefs:
    """Collect explicit blob refs from a source run and prove retained bytes.

    Blob field names come from admitted plugin configs, not a loose recursive
    search for hash-shaped strings. Transform-produced blob-fetch refs come
    from their typed success reason; PDF output refs come from typed render
    receipts. Each discovered blob must still exist with its matching hash.
    """
    fields = frozenset(blob_ref_fields)
    if any(type(field_name) is not str or not field_name for field_name in fields):
        raise ValueError("blob_ref_fields must contain admitted field names")
    input_refs: set[str] = set()
    output_refs: set[str] = set()
    pdf_node_ids = {node.node_id for node in factory.data_flow.get_nodes(source_run_id) if node.plugin_name == "pdf_rasterize"}
    pdf_state_ids: set[str] = set()
    pdf_completed_states: set[str] = set()
    pdf_receipt_states: set[str] = set()

    for batch in factory.query.iter_rows_for_run(source_run_id):
        for row in batch:
            data = factory.query.get_row_data(row.row_id)
            if data.state is not RowDataState.AVAILABLE or data.data is None:
                raise AuditIntegrityError(f"Source run {source_run_id}: row {row.row_id} payload unavailable")
            input_refs.update(_row_refs(data.data, fields=fields, location=f"row {row.row_id}"))

    for token in factory.query.get_all_tokens_for_run(source_run_id):
        if token.token_data_ref is None:
            continue
        token_data = source_store.retrieve(token.token_data_ref)
        try:
            decoded = json.loads(token_data)
        except (UnicodeDecodeError, ValueError) as exc:
            raise AuditIntegrityError(f"Source run {source_run_id}: token {token.token_id} payload is invalid JSON") from exc
        input_refs.update(_row_refs(decoded, fields=fields, location=f"token {token.token_id}"))

    for state in factory.query.get_all_node_states_for_run(source_run_id):
        if state.node_id in pdf_node_ids:
            pdf_state_ids.add(state.state_id)
        if isinstance(state, NodeStateCompleted) and state.node_id in pdf_node_ids:
            pdf_completed_states.add(state.state_id)
        reason_json = state.success_reason_json if isinstance(state, NodeStateCompleted) else None
        if reason_json is None:
            continue
        try:
            reason = json.loads(reason_json)
        except (UnicodeDecodeError, ValueError) as exc:
            raise AuditIntegrityError(f"Source run {source_run_id}: state {state.state_id} success reason is invalid JSON") from exc
        if isinstance(reason, dict) and isinstance(reason.get("metadata"), dict) and "fetch_payload_hash" in reason["metadata"]:
            metadata = reason.get("metadata")
            if type(metadata) is not dict or "fetch_payload_hash" not in metadata:
                raise AuditIntegrityError(f"Source run {source_run_id}: blob fetch state {state.state_id} has no payload hash")
            ref = _audited_ref(metadata["fetch_payload_hash"], location=f"state {state.state_id}")
            input_refs.add(ref)
            output_refs.add(ref)
        if isinstance(reason, dict) and isinstance(reason.get("metadata"), dict) and "fetch_response_processed_hash" in reason["metadata"]:
            metadata = reason["metadata"]
            output_refs.add(_audited_ref(metadata["fetch_response_processed_hash"], location=f"state {state.state_id}"))

    for call in factory.query.get_all_calls_for_run(source_run_id):
        if call.state_id not in pdf_state_ids:
            continue
        if call.call_type is not CallType.FILESYSTEM or call.status is not CallStatus.SUCCESS:
            raise AuditIntegrityError(f"Source run {source_run_id}: PDF call {call.call_id} is not a successful filesystem receipt")
        response = factory.execution.get_call_response_data(call.call_id)
        if response.state is not CallDataState.AVAILABLE or response.data is None:
            raise AuditIntegrityError(f"Source run {source_run_id}: PDF call {call.call_id} has no retained render receipt")
        receipt: Any = deep_thaw(response.data)
        if type(receipt) is not dict or receipt.get("format") != "pdf_rasterize/v1":
            raise AuditIntegrityError(f"Source run {source_run_id}: PDF state {call.state_id} has no typed render receipt")
        identity = receipt.get("renderer_identity")
        if type(identity) is not str or _HASH.fullmatch(identity) is None:
            raise AuditIntegrityError(f"Source run {source_run_id}: PDF call {call.call_id} has no renderer identity")
        if receipt.get("outcome_kind") not in ("rasterized", "document_refusal", "timeout"):
            raise AuditIntegrityError(f"Source run {source_run_id}: PDF call {call.call_id} has no typed worker outcome")
        if receipt.get("result_status") not in ("success", "error") or type(receipt.get("rows")) is not list:
            raise AuditIntegrityError(f"Source run {source_run_id}: PDF call {call.call_id} has no typed result")
        if call.state_id in pdf_receipt_states:
            raise AuditIntegrityError(f"Source run {source_run_id}: PDF state {call.state_id} has multiple render receipts")
        if call.state_id is not None:
            pdf_receipt_states.add(call.state_id)
        rendered = receipt.get("rendered")
        if type(rendered) is not list:
            raise AuditIntegrityError(f"Source run {source_run_id}: PDF call {call.call_id} has malformed rendered pages")
        for index, page in enumerate(rendered):
            if type(page) is not dict or "page_ref" not in page:
                raise AuditIntegrityError(f"Source run {source_run_id}: PDF call {call.call_id} page {index} has no payload ref")
            if page["page_ref"] is None and receipt.get("result_status") == "error":
                continue
            output_refs.add(_audited_ref(page["page_ref"], location=f"PDF call {call.call_id} page {index}"))

    if pdf_completed_states != pdf_receipt_states:
        raise AuditIntegrityError(
            f"Source run {source_run_id}: completed PDF states without typed render receipts: {sorted(pdf_completed_states - pdf_receipt_states)}"
        )

    for ref in input_refs | output_refs:
        content = source_store.retrieve(ref)
        if hashlib.sha256(content).hexdigest() != ref:
            raise AuditIntegrityError(f"Source run {source_run_id}: blob {ref} failed integrity check")
    return SourcePayloadRefs(input_refs=frozenset(input_refs), output_refs=frozenset(output_refs))


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
        original = self.read_output(content_hash)
        stored_ref = self._current_store.store(original)
        if stored_ref != content_hash:
            raise IntegrityError("New-run audit store returned a different output hash")
        return stored_ref

    def read_output(self, content_hash: str) -> bytes:
        """Read a retained source output only when the audit allowlist names it."""
        if content_hash not in self._output_refs:
            raise IntegrityError("Output reference is absent from source-run audit evidence")
        original = self._source_store.retrieve(content_hash)
        self._check_hash(content_hash, original)
        return original

    def store(self, content: bytes) -> str:
        if self._mode is RunMode.REPLAY:
            content_hash = hashlib.sha256(content).hexdigest()
            if content_hash not in self._output_refs:
                raise IntegrityError("Replay output has no source-run payload evidence")
            if self.read_output(content_hash) != content:
                raise IntegrityError("Replay output bytes differ from source-run evidence")
            stored_ref = self._current_store.store(content)
            if stored_ref != content_hash:
                raise IntegrityError("New-run audit store returned a different output hash")
            return stored_ref
        return self._current_store.store(content)

    def exists(self, content_hash: str) -> bool:
        return content_hash in self._source_refs and self._source_store.exists(content_hash)

    def delete(self, content_hash: str) -> bool:
        raise IntegrityError("A replay/verify run cannot delete payload evidence")

    @staticmethod
    def _check_hash(content_hash: str, content: bytes) -> None:
        if hashlib.sha256(content).hexdigest() != content_hash:
            raise IntegrityError("Payload content does not match source-run hash")
