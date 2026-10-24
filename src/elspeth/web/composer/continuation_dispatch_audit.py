"""Portable audit DATA and re-admission by the actual dispatch audit owner.

Captured-segment latency excludes time between capture and re-admission. It is
not a segment deadline, provider duration, current fence or execution grant.
The existing live DispatchAudit is never modified or replaced by capture.
This owner supports state-backed B2 dispatches with version_before >= 1.
Standalone DispatchAudit values at version zero remain outside this boundary;
the generic begin_dispatch owner accepts them, but portable capture refuses.
The capture-to-readmission gap, including checkpoint work after capture and
paused child intervals, is excluded. Committed writer timing is never replaced.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall, _AdmittedToolFunction
from elspeth.web.composer.audit import DispatchAudit, begin_dispatch
from elspeth.web.composer.authority_hashing import composer_authority_canonical_json, restore_composer_authority_payload
from elspeth.web.composer.continuation_result_credentials import require_retained_argument_credentials
from elspeth.web.composer.continuation_tool_batch import _read_json_value
from elspeth.web.composer.state import CompositionState
from elspeth.web.credential_guard import require_no_credential_material


class PortableDispatchAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.dispatch-audit-active.v1"]
    timing_semantics: Literal["captured_dispatch_segments"]
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments_canonical: str
    arguments_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    version_before: int = Field(ge=1)
    started_at: datetime
    elapsed_active_ns: int = Field(ge=0)
    actor: str = Field(min_length=1)
    authority_arguments_canonical: str | None
    authority_arguments_hash: str | None
    strict_sent: bool | None
    wire_conformant: bool | None


def _binding(audit: DispatchAudit | PortableDispatchAudit) -> tuple[object, ...]:
    return (
        audit.tool_call_id,
        audit.tool_name,
        audit.arguments_canonical,
        audit.arguments_hash,
        audit.version_before,
        audit.actor,
        audit.authority_arguments_canonical,
        audit.authority_arguments_hash,
        audit.strict_sent,
        audit.wire_conformant,
    )


def _admit(value: PortableDispatchAudit, env_ref_names: frozenset[str]) -> PortableDispatchAudit:
    if type(value) is not PortableDispatchAudit:
        raise TypeError("portable audit requires its exact owned value")
    encoded = canonical_json(value.model_dump(mode="json", warnings="error"))
    try:
        admitted = PortableDispatchAudit.model_validate_json(encoded, strict=True)
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("portable audit violates its complete owned contract") from error
    if canonical_json(admitted.model_dump(mode="json", warnings="error")) != encoded:
        raise AuditIntegrityError("portable audit lost or normalized a field")
    if admitted.started_at.utcoffset() != timedelta(0):
        raise AuditIntegrityError("portable audit must retain its original aware UTC start")
    arguments = _read_json_value(admitted.arguments_canonical)
    if type(arguments) is not dict or sha256(admitted.arguments_canonical.encode("utf-8")).hexdigest() != admitted.arguments_hash:
        raise AuditIntegrityError("portable audit argument hash differs from exact canonical bytes")
    if (admitted.authority_arguments_canonical is None) != (admitted.authority_arguments_hash is None):
        raise AuditIntegrityError("portable audit lost its paired authority binding")
    if admitted.authority_arguments_canonical is not None:
        if admitted.tool_name != "set_pipeline":
            raise AuditIntegrityError("portable audit authority binding names another tool")
        _read_json_value(admitted.authority_arguments_canonical)
        if sha256(admitted.authority_arguments_canonical.encode("utf-8")).hexdigest() != admitted.authority_arguments_hash:
            raise AuditIntegrityError("portable audit authority hash differs from exact canonical bytes")
    require_retained_argument_credentials(admitted.tool_name, arguments, env_ref_names=env_ref_names)
    # Re-enter the actual order-bearing argument domain before credential
    # inspection; the projected sources/branches wrappers are not components.
    if admitted.authority_arguments_canonical is not None:
        projection = _read_json_value(admitted.authority_arguments_canonical)
        if type(projection) is not dict:
            raise AuditIntegrityError("portable audit authority projection is not its owned object")
        try:
            restored = restore_composer_authority_payload(projection)
        except (TypeError, ValueError) as error:
            raise AuditIntegrityError("portable audit authority projection is malformed") from error
        if (
            canonical_json(restored) != admitted.arguments_canonical
            or composer_authority_canonical_json(restored) != admitted.authority_arguments_canonical
        ):
            raise AuditIntegrityError("portable audit authority projection differs from complete arguments")
        require_retained_argument_credentials(admitted.tool_name, restored, env_ref_names=env_ref_names)
    require_no_credential_material(
        [admitted.tool_call_id, admitted.tool_name, admitted.actor],
        surface="retained dispatch audit identities",
        env_ref_names=env_ref_names,
    )
    return admitted


def capture_dispatch_audit(audit: DispatchAudit, *, env_ref_names: frozenset[str] = frozenset()) -> PortableDispatchAudit:
    if type(audit) is not DispatchAudit or type(audit.started_ns) is not int:
        raise TypeError("portable capture requires the actual live dispatch audit")
    now = time.monotonic_ns()
    elapsed = now - audit.started_ns
    if elapsed < 0:
        # A foreign/future anchor is not evidence of zero elapsed work.
        raise AuditIntegrityError("dispatch audit anchor is ahead of the current monotonic observation")
    value = PortableDispatchAudit(
        schema_version="composer.dispatch-audit-active.v1",
        timing_semantics="captured_dispatch_segments",
        tool_call_id=audit.tool_call_id,
        tool_name=audit.tool_name,
        arguments_canonical=audit.arguments_canonical,
        arguments_hash=audit.arguments_hash,
        version_before=audit.version_before,
        started_at=audit.started_at,
        elapsed_active_ns=elapsed,
        actor=audit.actor,
        authority_arguments_canonical=audit.authority_arguments_canonical,
        authority_arguments_hash=audit.authority_arguments_hash,
        strict_sent=audit.strict_sent,
        wire_conformant=audit.wire_conformant,
    )
    return _admit(value, env_ref_names)


def encode_dispatch_audit(value: PortableDispatchAudit, *, env_ref_names: frozenset[str] = frozenset()) -> str:
    admitted = _admit(value, env_ref_names)
    return canonical_json(admitted.model_dump(mode="json", warnings="error"))


def decode_dispatch_audit(encoded: str, *, env_ref_names: frozenset[str] = frozenset()) -> PortableDispatchAudit:
    _read_json_value(encoded)
    try:
        value = PortableDispatchAudit.model_validate_json(encoded, strict=True)
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("retained dispatch audit violates its complete owned value") from error
    if encode_dispatch_audit(value, env_ref_names=env_ref_names) != encoded:
        raise AuditIntegrityError("retained dispatch audit omitted or normalized a field")
    return value


def re_admit_dispatch_audit(
    value: PortableDispatchAudit,
    *,
    selected_call: _AdmittedToolCall,
    pre_state: CompositionState,
    audit_arguments: Mapping[str, Any] | str,
    actor: str,
    strict_sent: bool | None,
    wire_conformant: bool | None,
    env_ref_names: frozenset[str] = frozenset(),
) -> DispatchAudit:
    """Rebuild only a timer after the actual audit owner rederives its binding.

    Callers must independently bind these original call/pre-state/arguments to
    their selected checkpoint and fresh current authority. This function does
    not authorize use, replay a tool, or make a new current session context.
    It returns an audit; it never copies or mutates the supplied state/call.
    """
    admitted = _admit(value, env_ref_names)
    if (
        type(selected_call) is not _AdmittedToolCall
        or type(selected_call.function) is not _AdmittedToolFunction
        or type(pre_state) is not CompositionState
    ):
        raise TypeError("audit re-admission requires actual selected call and pre-state owners")
    fresh = begin_dispatch(
        selected_call.id,
        selected_call.function.name,
        audit_arguments,
        version_before=pre_state.version,
        actor=actor,
        strict_sent=strict_sent,
        wire_conformant=wire_conformant,
    )
    if _binding(fresh) != _binding(admitted):
        raise AuditIntegrityError("retained audit differs from the actual selected audit binding")
    # No origin of the previous process's monotonic clock is reused. A
    # synthetic negative anchor is valid when retained active time exceeds
    # this process's current monotonic reading: only its local difference is
    # consumed by the unchanged finish_* owner.
    return replace(fresh, started_at=admitted.started_at, started_ns=fresh.started_ns - admitted.elapsed_active_ns)
