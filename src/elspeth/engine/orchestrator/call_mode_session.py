"""Parent-bound audit call session for replay and live verification."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from elspeth.contracts.call_mode import (
    ArchivedCallRequestEvidence,
    ReplayCallEvidence,
    ReplaySSRFRequest,
    SourceCallParentIdentity,
    VerificationDecision,
)
from elspeth.contracts.enums import CallStatus, CallType, RunMode
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.core.landscape.row_data import CallDataState

if TYPE_CHECKING:
    from elspeth.contracts.audit import Call
    from elspeth.core.landscape.factory import RecorderFactory


_FINGERPRINTED_AUTH = re.compile(r"<fingerprint:[0-9a-f]{64}>\Z")


def _require_archived_dns_pin(data: Mapping[str, Any]) -> None:
    resolved_ip = data.get("resolved_ip")
    if type(resolved_ip) is not str or not resolved_ip:
        raise AuditIntegrityError("Source HTTP request lacks an archived DNS pin")
    try:
        ip_address(resolved_ip)
    except ValueError as exc:
        raise AuditIntegrityError("Source HTTP request has an invalid archived DNS pin") from exc


def _mi_non_auth_request(data: Mapping[str, Any], *, before_dns: bool) -> dict[str, object]:
    """Preserve every HTTP request field except the rotating MI fingerprint."""
    raw_headers = data.get("headers")
    if not isinstance(raw_headers, Mapping):
        raise AuditIntegrityError("Managed-identity HTTP request has no structured headers")
    auth_names = [name for name in raw_headers if isinstance(name, str) and name.lower() == "authorization"]
    if len(auth_names) > 1:
        raise AuditIntegrityError("Managed-identity HTTP request has ambiguous Authorization headers")
    if not before_dns:
        if len(auth_names) != 1 or not isinstance(raw_headers[auth_names[0]], str):
            raise AuditIntegrityError("Managed-identity HTTP request lacks fingerprinted Authorization")
        if _FINGERPRINTED_AUTH.fullmatch(raw_headers[auth_names[0]]) is None:
            raise AuditIntegrityError("Managed-identity HTTP Authorization is not a fingerprint")
    comparison = dict(data)
    comparison["headers"] = {name: value for name, value in raw_headers.items() if name not in auth_names}
    if before_dns:
        comparison.pop("resolved_ip", None)
    return comparison


class AuditedCallModeSession:
    """One run's source-call authority, with no transport of its own.

    An adapter must record each replayed call with ``source_call_id`` returned
    here. In verify mode it first records the live call, then persists the
    comparison verdict through ``verify_call``. A missing or incomplete source
    call never becomes permission to dispatch a live replay call.
    """

    def __init__(self, factory: RecorderFactory, *, current_run_id: str, source_run_id: str, mode: RunMode) -> None:
        if mode is RunMode.LIVE:
            raise ValueError("A call-mode session requires replay or verify mode")
        self._factory = factory
        self._current_run_id = current_run_id
        self._source_run_id = source_run_id
        self._mode = mode
        self._consumed: set[str] = set()
        self._failed_decisions: set[str] = set()
        self._verify_admissions: dict[tuple[CallType, str | None, str | None, int | None], str] = {}
        self._mi_verify_admissions: set[tuple[CallType, str | None, str | None, int]] = set()
        self._mi_preflights: dict[tuple[str | None, str | None, str], str] = {}
        self._required = self._preflight_source_calls()

    @property
    def mode(self) -> RunMode:
        return self._mode

    @property
    def source_run_id(self) -> str:
        return self._source_run_id

    def _preflight_source_calls(self) -> frozenset[str]:
        """Reject incomplete call archives before any plugin lifecycle hook."""
        calls = self._factory.query.get_all_calls_for_run(self._source_run_id)
        calls.extend(self._factory.execution.get_all_operation_calls_for_run(self._source_run_id))
        required: set[str] = set()
        for call in calls:
            if call.operation_id is not None:
                operation = self._factory.execution.get_operation(call.operation_id)
                if operation is None:
                    raise AuditIntegrityError(f"Source operation for call {call.call_id} disappeared")
                if operation.operation_type != "runtime_preflight":
                    continue
            request = self._factory.execution.get_call_request_data(call.call_id)
            if request.state is not CallDataState.AVAILABLE:
                raise AuditIntegrityError(f"Source call {call.call_id} has no complete request archive")
            response = self._factory.execution.get_call_response_data(call.call_id)
            if call.status is CallStatus.SUCCESS and response.state is not CallDataState.AVAILABLE:
                raise AuditIntegrityError(f"Source call {call.call_id} has no complete response archive")
            if call.status is CallStatus.ERROR:
                if response.state not in (CallDataState.AVAILABLE, CallDataState.NEVER_STORED):
                    raise AuditIntegrityError(f"Source error call {call.call_id} has incomplete response archive")
                if call.error_json is None:
                    raise AuditIntegrityError(f"Source error call {call.call_id} lacks structured error evidence")
                parsed_error = json.loads(call.error_json)
                if type(parsed_error) is not dict:
                    raise AuditIntegrityError(f"Source error call {call.call_id} lacks structured error evidence")
                if call.call_type is CallType.LLM and parsed_error.get("category") not in {
                    "rate_limit",
                    "content_policy",
                    "context_length",
                    "server",
                    "network",
                    "client",
                    "unknown",
                    "response_processing",
                }:
                    raise AuditIntegrityError(f"Source LLM error call {call.call_id} lacks owned error category")
            required.add(call.call_id)
        return frozenset(required)

    def _source_call(
        self,
        *,
        call_type: CallType,
        request_data: Mapping[str, Any],
        current_state_id: str | None,
        current_operation_id: str | None,
        current_call_index: int,
    ) -> Call:
        call = self._factory.execution.find_call_for_current_parent(
            source_run_id=self._source_run_id,
            call_type=call_type,
            request_hash=stable_hash(request_data),
            current_state_id=current_state_id,
            current_operation_id=current_operation_id,
            current_call_index=current_call_index,
        )
        if call is None:
            raise AuditIntegrityError(f"No exact source {call_type.value} call at parent-local index {current_call_index}")
        if call.call_id in self._consumed:
            raise AuditIntegrityError(f"Source call {call.call_id} was already consumed")
        return call

    def replay_call(
        self,
        *,
        call_type: CallType,
        request_data: Mapping[str, Any],
        current_state_id: str | None,
        current_operation_id: str | None,
        current_call_index: int,
    ) -> ReplayCallEvidence:
        if self._mode is not RunMode.REPLAY:
            raise AuditIntegrityError("Replay call requested outside replay mode")
        call = self._source_call(
            call_type=call_type,
            request_data=request_data,
            current_state_id=current_state_id,
            current_operation_id=current_operation_id,
            current_call_index=current_call_index,
        )
        response = self._factory.execution.get_call_response_data(call.call_id)
        if call.status is CallStatus.SUCCESS and response.state is not CallDataState.AVAILABLE:
            raise AuditIntegrityError(f"Source call {call.call_id} has no complete retained response")
        if call.status is CallStatus.ERROR and response.state not in (CallDataState.AVAILABLE, CallDataState.NEVER_STORED):
            raise AuditIntegrityError(f"Source error call {call.call_id} has incomplete retained response")
        error_data: Mapping[str, Any] | None = None
        if call.error_json is not None:
            parsed = json.loads(call.error_json)
            if type(parsed) is not dict:
                raise AuditIntegrityError(f"Source call {call.call_id} error evidence is malformed")
            error_data = parsed
        if call.status is CallStatus.ERROR and error_data is None:
            raise AuditIntegrityError(f"Source error call {call.call_id} has no reconstructable error evidence")
        self._consumed.add(call.call_id)
        return ReplayCallEvidence(
            source_call_id=call.call_id,
            status=call.status,
            response_data=response.data,
            error_data=error_data,
            latency_ms=call.latency_ms,
        )

    def replay_ssrf_request(
        self,
        *,
        original_url: str,
        call_type: CallType,
        current_state_id: str | None,
        current_operation_id: str | None,
    ) -> ReplaySSRFRequest:
        if self._mode is not RunMode.REPLAY:
            raise AuditIntegrityError("Archived DNS pin requested outside replay mode")
        matches: set[str] = set()
        for call in self._factory.execution.list_source_calls_for_current_parent(
            source_run_id=self._source_run_id,
            call_type=call_type,
            current_state_id=current_state_id,
            current_operation_id=current_operation_id,
        ):
            request = self._factory.execution.get_call_request_data(call.call_id)
            if request.state is not CallDataState.AVAILABLE or request.data is None:
                raise AuditIntegrityError(f"Source call {call.call_id} has no retained request for DNS replay")
            if request.data.get("url") == original_url and request.data.get("hop_number") is None:
                pin = request.data.get("resolved_ip")
                if type(pin) is not str or not pin:
                    raise AuditIntegrityError(f"Source call {call.call_id} lacks a DNS pin")
                matches.add(pin)
        if len(matches) != 1:
            raise AuditIntegrityError(f"Archived DNS pin for {original_url!r} is missing or ambiguous")
        parsed = urlsplit(original_url)
        hostname = parsed.hostname
        if hostname is None:
            raise AuditIntegrityError("Archived DNS URL has no hostname")
        scheme = parsed.scheme.lower()
        port = parsed.port or (443 if scheme == "https" else 80)
        default_port = 443 if scheme == "https" else 80
        host_for_header = f"[{hostname}]" if ":" in hostname else hostname
        host_header = f"{host_for_header}:{port}" if port != default_port else host_for_header
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        if parsed.fragment:
            path = f"{path}#{parsed.fragment}"
        return ReplaySSRFRequest(
            original_url=original_url,
            resolved_ip=next(iter(matches)),
            host_header=host_header,
            port=port,
            path=path,
            scheme=scheme,
            bare_hostname=hostname,
        )

    def source_parent_identity(
        self,
        *,
        call_type: CallType,
        current_state_id: str | None,
        current_operation_id: str | None,
    ) -> SourceCallParentIdentity:
        calls = self._factory.execution.list_source_calls_for_current_parent(
            source_run_id=self._source_run_id,
            call_type=call_type,
            current_state_id=current_state_id,
            current_operation_id=current_operation_id,
        )
        parents = {(call.state_id, call.operation_id) for call in calls}
        if len(parents) != 1:
            raise AuditIntegrityError("Source call parent is missing or ambiguous")
        source_state_id, source_operation_id = next(iter(parents))
        if source_state_id is not None:
            state = self._factory.execution.get_node_state(source_state_id)
            if state is None:
                raise AuditIntegrityError("Source call state disappeared")
            return SourceCallParentIdentity(
                source_run_id=self._source_run_id,
                source_node_id=state.node_id,
                source_state_id=source_state_id,
                source_operation_id=None,
                source_token_id=state.token_id,
            )
        if source_operation_id is None:
            raise AuditIntegrityError("Source call has no operation parent")
        operation = self._factory.execution.get_operation(source_operation_id)
        if operation is None:
            raise AuditIntegrityError("Source call operation disappeared")
        return SourceCallParentIdentity(
            source_run_id=self._source_run_id,
            source_node_id=operation.node_id,
            source_state_id=None,
            source_operation_id=source_operation_id,
            source_token_id=None,
        )

    def archived_call_request(
        self,
        *,
        call_type: CallType,
        current_state_id: str | None,
        current_operation_id: str | None,
        current_call_index: int,
    ) -> ArchivedCallRequestEvidence:
        call = self._factory.execution.find_call_for_current_parent(
            source_run_id=self._source_run_id,
            call_type=call_type,
            request_hash=None,
            current_state_id=current_state_id,
            current_operation_id=current_operation_id,
            current_call_index=current_call_index,
        )
        if call is None:
            raise AuditIntegrityError("Exact source request is missing")
        request = self._factory.execution.get_call_request_data(call.call_id)
        if request.state is not CallDataState.AVAILABLE or request.data is None:
            raise AuditIntegrityError(f"Source request {call.call_id} is unavailable")
        return ArchivedCallRequestEvidence(source_call_id=call.call_id, request_data=request.data)

    def admit_verify_call(
        self,
        *,
        call_type: CallType,
        request_data: Mapping[str, Any],
        current_state_id: str | None,
        current_operation_id: str | None,
        current_call_index: int | None,
    ) -> str:
        if self._mode is not RunMode.VERIFY:
            raise AuditIntegrityError("Verification admission requested outside verify mode")
        if current_call_index is None:
            candidates = [
                candidate
                for candidate in self._factory.execution.list_source_calls_for_current_parent(
                    source_run_id=self._source_run_id,
                    call_type=call_type,
                    current_state_id=current_state_id,
                    current_operation_id=current_operation_id,
                )
                if candidate.request_hash == stable_hash(request_data)
                and candidate.call_id not in self._consumed
                and candidate.call_id not in self._verify_admissions.values()
            ]
            if len(candidates) != 1:
                raise AuditIntegrityError("Verification source request is missing or ambiguous before live dispatch")
            call = candidates[0]
        else:
            call = self._source_call(
                call_type=call_type,
                request_data=request_data,
                current_state_id=current_state_id,
                current_operation_id=current_operation_id,
                current_call_index=current_call_index,
            )
        key = (call_type, current_state_id, current_operation_id, current_call_index)
        if key in self._verify_admissions:
            raise AuditIntegrityError("Verification call was already admitted")
        self._verify_admissions[key] = call.call_id
        return call.call_id

    def preflight_verify_request(
        self,
        *,
        call_type: CallType,
        request_data: Mapping[str, Any],
        current_state_id: str | None,
        current_operation_id: str | None,
    ) -> str:
        """Find one semantic source request without allocating a current call index."""
        if self._mode is not RunMode.VERIFY:
            raise AuditIntegrityError("Verification preflight requested outside verify mode")
        candidates = [
            call
            for call in self._factory.execution.list_source_calls_for_current_parent(
                source_run_id=self._source_run_id,
                call_type=call_type,
                current_state_id=current_state_id,
                current_operation_id=current_operation_id,
            )
            if call.request_hash == stable_hash(request_data) and call.call_id not in self._consumed
        ]
        if len(candidates) != 1:
            raise AuditIntegrityError("Verification source request is missing or ambiguous before nested transport")
        candidate = candidates[0]
        archived = self._factory.execution.get_call_request_data(candidate.call_id)
        if archived.state is not CallDataState.AVAILABLE or archived.data is None:
            raise AuditIntegrityError("Verification source request payload is unavailable before nested transport")
        if stable_hash(archived.data) != stable_hash(request_data):
            raise AuditIntegrityError("Verification source request payload disagrees with current request")
        return candidate.call_id

    def preflight_verify_http_managed_identity(
        self,
        *,
        request_data: Mapping[str, Any],
        current_state_id: str | None,
        current_operation_id: str | None,
    ) -> ArchivedCallRequestEvidence:
        if self._mode is not RunMode.VERIFY:
            raise AuditIntegrityError("Managed-identity HTTP preflight requires verify mode")
        current = _mi_non_auth_request(request_data, before_dns=True)
        candidates: list[ArchivedCallRequestEvidence] = []
        for call in self._factory.execution.list_source_calls_for_current_parent(
            source_run_id=self._source_run_id,
            call_type=CallType.HTTP,
            current_state_id=current_state_id,
            current_operation_id=current_operation_id,
        ):
            archived = self._factory.execution.get_call_request_data(call.call_id)
            if archived.state is not CallDataState.AVAILABLE or archived.data is None:
                raise AuditIntegrityError(f"Source HTTP request {call.call_id} is unavailable")
            _require_archived_dns_pin(archived.data)
            source = _mi_non_auth_request(archived.data, before_dns=False)
            source.pop("resolved_ip", None)
            if source == current:
                candidates.append(ArchivedCallRequestEvidence(source_call_id=call.call_id, request_data=archived.data))
        if len(candidates) != 1:
            raise AuditIntegrityError("Managed-identity source request is missing or ambiguous before token acquisition")
        url = request_data.get("url")
        if type(url) is not str:
            raise AuditIntegrityError("Managed-identity HTTP preflight requires a URL")
        self._mi_preflights[(current_state_id, current_operation_id, url)] = candidates[0].source_call_id
        return candidates[0]

    def preflight_verify_http_request(
        self,
        *,
        request_data: Mapping[str, Any],
        current_state_id: str | None,
        current_operation_id: str | None,
    ) -> ArchivedCallRequestEvidence:
        if self._mode is not RunMode.VERIFY:
            raise AuditIntegrityError("HTTP preflight requires verify mode")
        current = dict(request_data)
        current.pop("resolved_ip", None)
        candidates: list[ArchivedCallRequestEvidence] = []
        for call in self._factory.execution.list_source_calls_for_current_parent(
            source_run_id=self._source_run_id,
            call_type=CallType.HTTP,
            current_state_id=current_state_id,
            current_operation_id=current_operation_id,
        ):
            archived = self._factory.execution.get_call_request_data(call.call_id)
            if archived.state is not CallDataState.AVAILABLE or archived.data is None:
                raise AuditIntegrityError(f"Source HTTP request {call.call_id} is unavailable")
            _require_archived_dns_pin(archived.data)
            source = dict(archived.data)
            source.pop("resolved_ip", None)
            if source == current:
                candidates.append(ArchivedCallRequestEvidence(source_call_id=call.call_id, request_data=archived.data))
        if len(candidates) != 1:
            raise AuditIntegrityError("HTTP source request is missing or ambiguous before DNS")
        return candidates[0]

    def admit_verify_http_managed_identity(
        self,
        *,
        request_data: Mapping[str, Any],
        current_state_id: str | None,
        current_operation_id: str | None,
        current_call_index: int,
        source_call_id: str,
    ) -> str:
        if self._mode is not RunMode.VERIFY:
            raise AuditIntegrityError("Managed-identity HTTP admission requires verify mode")
        source = self.archived_call_request(
            call_type=CallType.HTTP,
            current_state_id=current_state_id,
            current_operation_id=current_operation_id,
            current_call_index=current_call_index,
        )
        if source.source_call_id != source_call_id:
            raise AuditIntegrityError("Managed-identity source call changed after preflight")
        url = request_data.get("url")
        if type(url) is not str or self._mi_preflights.pop((current_state_id, current_operation_id, url), None) != source_call_id:
            raise AuditIntegrityError("Managed-identity HTTP call has no pre-token source admission")
        if _mi_non_auth_request(source.request_data, before_dns=False) != _mi_non_auth_request(request_data, before_dns=False):
            raise AuditIntegrityError("Managed-identity HTTP non-auth request fields differ")
        key = (CallType.HTTP, current_state_id, current_operation_id, current_call_index)
        if key in self._verify_admissions:
            raise AuditIntegrityError("Managed-identity HTTP call was already admitted")
        self._verify_admissions[key] = source_call_id
        self._mi_verify_admissions.add(key)
        return source_call_id

    def verify_call(
        self,
        *,
        call_type: CallType,
        request_data: Mapping[str, Any],
        current_state_id: str | None,
        current_operation_id: str | None,
        current_call_index: int,
        current_call_id: str,
        live_status: CallStatus,
        live_response_data: Mapping[str, Any] | None,
        live_error_data: Mapping[str, Any] | None,
    ) -> VerificationDecision:
        if self._mode is not RunMode.VERIFY:
            raise AuditIntegrityError("Verification requested outside verify mode")
        key = (call_type, current_state_id, current_operation_id, current_call_index)
        admitted_source_call_id = self._verify_admissions.pop(key, None)
        if admitted_source_call_id is None:
            admitted_source_call_id = self._verify_admissions.pop((call_type, current_state_id, current_operation_id, None), None)
        if admitted_source_call_id is None:
            raise AuditIntegrityError("Verification call reached settlement without pre-dispatch source admission")
        semantic_mi = key in self._mi_verify_admissions
        self._mi_verify_admissions.discard(key)
        call = self._factory.execution.find_call_for_current_parent(
            source_run_id=self._source_run_id,
            call_type=call_type,
            request_hash=None if semantic_mi else stable_hash(request_data),
            current_state_id=current_state_id,
            current_operation_id=current_operation_id,
            current_call_index=current_call_index,
        )
        if call is not None and call.call_id in self._consumed:
            raise AuditIntegrityError(f"Source call {call.call_id} was already consumed")
        if call is None or call.call_id != admitted_source_call_id:
            raise AuditIntegrityError("Verification source call changed after pre-dispatch admission")
        differences: dict[str, Any] = {}
        is_match: bool | None = None
        if call is not None:
            source_response = self._factory.execution.get_call_response_data(call.call_id)
            if source_response.state is CallDataState.AVAILABLE:
                recorded_response = source_response.data
            elif source_response.state is CallDataState.NEVER_STORED and call.status is CallStatus.ERROR:
                recorded_response = None
            else:
                recorded_response = None
                differences["source_response"] = source_response.state.value
            recorded_error = json.loads(call.error_json) if call.error_json is not None else None
            if not differences:
                if call.status is not live_status:
                    differences["status"] = {"source": call.status.value, "current": live_status.value}
                if stable_hash(recorded_response) != stable_hash(live_response_data):
                    differences["response_hash"] = {
                        "source": stable_hash(recorded_response),
                        "current": stable_hash(live_response_data),
                    }
                if stable_hash(recorded_error) != stable_hash(live_error_data):
                    differences["error_hash"] = {
                        "source": stable_hash(recorded_error),
                        "current": stable_hash(live_error_data),
                    }
                is_match = not differences
            self._consumed.add(call.call_id)
        self._factory.execution.record_verification_decision(
            current_run_id=self._current_run_id,
            current_call_id=current_call_id,
            source_run_id=self._source_run_id,
            source_call_id=call.call_id if call is not None else None,
            is_match=is_match,
            differences_json=canonical_json(differences),
        )
        if is_match is not True:
            self._failed_decisions.add(current_call_id)
        return VerificationDecision(
            current_call_id=current_call_id,
            source_call_id=call.call_id if call is not None else None,
            is_match=is_match,
            differences=deep_thaw(differences),
        )

    def finalize(self) -> None:
        """Require every state call and runtime-preflight call to be consumed."""
        missing = self._required - self._consumed
        if missing or self._failed_decisions or self._verify_admissions:
            raise AuditIntegrityError(
                f"{self._mode.value} call verification incomplete: {len(missing)} unconsumed source calls, "
                f"{len(self._failed_decisions)} failed decisions, {len(self._verify_admissions)} unsettled admissions"
            )
