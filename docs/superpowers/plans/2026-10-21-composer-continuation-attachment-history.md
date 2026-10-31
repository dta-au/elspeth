# Composer Continuation Attachment History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the exact successful complete-original attachment inspections performed by actual retained reads, without allowing history to replace a fresh inspection on a later use.

**Architecture:** H3c's existing locked reader captures its actual nominal inspection receipt in the current read-observations owner. The closed retained-read value carries that history through SQL retention. B5 applies it to the selected Compose frame in the same checkpoint CAS that consumes that read's pending owner result; cold decode creates historical values and never recreates an inspection capability.

**Tech Stack:** Python 3.12+, strict frozen Pydantic values, existing Composer private chunks, SQLAlchemy session authority, pytest and the existing SQLite/PostgreSQL fixtures.

---

This companion binds the unchanged blob-reader source in qualified candidate `2e869240a388d1ede0454f1fbb9b019d22a9a9da`, H3c in [heuristic cleanup](2026-10-20-composer-heuristic-cleanup.md), C3.8e in [storage](2026-10-20-composer-continuation-storage.md), B5 in [batches](2026-10-21-composer-continuation-batches.md), and F7/F8 in [frames](2026-10-21-composer-continuation-frames.md). Actual isolated integration has started; preserve its later dependency and owner handoffs. The commands below remain future checks for this attachment-history implementation; none ran while authoring this companion.

The receipt actually available from H3c is `AttachmentReadAdmission(session_id, blob_id, content_hash, size_bytes, detector_version, policy_fingerprint)`. It contains no timestamp or operation ID; this packet invents neither. The selected SQL read effect and outer logical-request binding provide operation custody. Two distinct reads retain two history entries even if their inspections have equal values. A repeated application of the same selected effect is idempotent only if its complete retained inspection tuple is identical.

## A0: Owners and dependencies

| File | Responsibility |
| --- | --- |
| Create `src/elspeth/web/composer/continuation_attachment_history.py` | Closed historical values, exact private codec, pure selected-pending-read application. |
| Modify `src/elspeth/web/sessions/continuation_owner_results.py` | Actual inspection observations in the existing read owner and current version/session validation. |
| Modify `src/elspeth/web/composer/tools/blobs.py` | Capture H3c's actual return inside its existing custody lock; every real use still calls the primitive. |
| Modify `src/elspeth/web/composer/continuation_tool_batch.py` | B5 calls the exact pure history hook before its existing owner-application CAS. |
| Modify `src/elspeth/web/composer/continuation_driver.py`, `continuation_execution.py` | F14's initializer creates explicit empty history once; F14 restore/F9 capture decode and preserve the selected historical chunk thereafter. |
| Create `tests/unit/web/composer/test_continuation_attachment_history.py` | Actual ingress/read/SQL/root application and refusal controls. |
| Modify `tests/unit/web/composer/test_continuation_evidence.py` | F7's actual fixture uses the new closed empty attachment history, not `{}`. |
| Modify `tests/unit/web/sessions/test_continuation_cold_restore.py` and `tests/testcontainer/web/test_composer_continuation_postgres.py` | Replace the old opaque attachment-codec test with exact closed history storage; retain actual read-version tests. |

H3c's actual `attachment_admission.py` must exist before A2. C1a supplies `ContinuationFrameStack`, `ComposeFrame`, `PrivateChunk`; C3.8e supplies the real read DTO/functions; B5 supplies `commit_owner_application`; F7 supplies the explicitly defined `new_evidence` and `pin_evidence` test helpers. These are already literal planned owners in the named companions, not proposed callbacks. The concrete current-use bridge is [producers T6–T10](2026-10-21-composer-continuation-producers.md#t6-define-the-closed-native-and-attachment-replay-producers); A2a below supplies its current actor-scoped inspection policy.

## A1: Closed historical values and codec

**Create:** `src/elspeth/web/composer/continuation_attachment_history.py`.

- [ ] Write A4's complete tests and add the importable A1 contracts before behavioral RED. Use A5's precise omitted-observation mutation to prove the actual successful inspection is required; missing modules and unrelated collection failures are setup failures.
- [ ] Add this complete module body. `RetainedAttachmentInspection` is a record of a past actual successful inspection. There is deliberately no conversion from it back to `AttachmentReadAdmission`, no `is_allowed` method, and no current detector/policy equality check that would confuse old history with present clearance.

```python
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.attachment_admission import AttachmentReadAdmission
from elspeth.web.composer.continuation import ComposeFrame, ContinuationFrameStack, PrivateChunkRef
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_private_chunk, verify_private_chunk

if TYPE_CHECKING:
    from elspeth.web.sessions.continuation_owner_results import ContinuationReadOwnerResult


class RetainedAttachmentInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    session_id: str = Field(min_length=1)
    blob_id: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    detector_version: str = Field(min_length=1)
    policy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_actual(cls, actual: AttachmentReadAdmission) -> Self:
        if type(actual) is not AttachmentReadAdmission:
            raise TypeError("inspection history requires the actual complete-original receipt")
        return cls(
            session_id=actual.session_id, blob_id=actual.blob_id,
            content_hash=actual.content_hash, size_bytes=actual.size_bytes,
            detector_version=actual.detector_version, policy_fingerprint=actual.policy_fingerprint,
        )


class RetainedAttachmentRead(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    effect_key: str = Field(min_length=1)
    inspections: tuple[RetainedAttachmentInspection, ...] = Field(min_length=1)


class ContinuationAttachmentHistory(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.attachment-admissions.v1"] = "composer.attachment-admissions.v1"
    session_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    reads: tuple[RetainedAttachmentRead, ...]

    @model_validator(mode="after")
    def require_history_scope(self) -> Self:
        keys = tuple(read.effect_key for read in self.reads)
        if len(keys) != len(set(keys)):
            raise ValueError("attachment history duplicates a read effect")
        if any(item.session_id != self.session_id for read in self.reads for item in read.inspections):
            raise ValueError("attachment history contains another session")
        return self


def empty_attachment_history(*, session_id: str, operation_id: str) -> ContinuationAttachmentHistory:
    return ContinuationAttachmentHistory(session_id=session_id, operation_id=operation_id, reads=())


def encode_attachment_history(history: ContinuationAttachmentHistory) -> PrivateChunk:
    if type(history) is not ContinuationAttachmentHistory:
        raise TypeError("attachment codec requires its complete nominal history")
    return encode_private_chunk(
        codec="composer.attachment-admissions.v1", canonical=canonical_json(history.model_dump(mode="json")),
    )


def decode_attachment_history(
    chunk: PrivateChunk, *, session_id: str, operation_id: str,
) -> ContinuationAttachmentHistory:
    if type(chunk) is not PrivateChunk or chunk.reference.codec != "composer.attachment-admissions.v1":
        raise AuditIntegrityError("attachment history has another private codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    try:
        result = ContinuationAttachmentHistory.model_validate_json(chunk.canonical, strict=True)
    except ValidationError as exc:
        raise AuditIntegrityError("attachment history violates its complete owned codec") from exc
    if canonical_json(result.model_dump(mode="json")) != chunk.canonical:
        raise AuditIntegrityError("attachment history lost fields during complete decode")
    if result.session_id != session_id or result.operation_id != operation_id:
        raise AuditIntegrityError("attachment history differs from the selected logical request")
    return result


def _strict_object(encoded: str, label: str) -> dict[str, Any]:
    value, error = parse_json_strict(encoded)
    if error is not None or type(value) is not dict:
        raise AuditIntegrityError(f"retained attachment {label} is not a strict object")
    return value


def require_read_attachment_history(
    result: ContinuationReadOwnerResult, *, expected_session_id: str | None = None,
) -> None:
    from elspeth.web.sessions.continuation_owner_results import ContinuationReadOwnerResult
    if type(result) is not ContinuationReadOwnerResult:
        raise TypeError("attachment history requires the selected nominal read owner")
    arguments = _strict_object(result.arguments_json, "arguments")
    response = _strict_object(result.response_json, "response")
    inspections = result.attachment_inspections
    text_read = result.tool_name in ("get_blob_content", "read_attachment")
    if not text_read:
        if inspections:
            raise AuditIntegrityError("a non-original-inspection read invented attachment inspections")
        return
    if type(response.get("success")) is not bool:
        raise AuditIntegrityError("retained attachment response has no exact success disposition")
    if response["success"] and not inspections:
        raise AuditIntegrityError("successful attachment read omitted its actual original inspection")
    if not inspections:
        return
    raw_blob_id = arguments.get("blob_id")
    if type(raw_blob_id) is not str:
        raise AuditIntegrityError("inspected attachment has no actual argument identity")
    try:
        target = UUID(raw_blob_id)
    except ValueError as exc:
        raise AuditIntegrityError("inspected attachment has an invalid actual argument identity") from exc
    versions = {version.blob_id: version for version in result.blob_versions}
    version = versions.get(target)
    if version is None or version.status != "ready":
        raise AuditIntegrityError("attachment inspection has no exact ready blob observation")
    for item in inspections:
        try:
            observed_id = UUID(item.blob_id)
        except ValueError as exc:
            raise AuditIntegrityError("attachment inspection has an invalid observed identity") from exc
        if (observed_id != target or item.content_hash != version.content_hash
            or item.size_bytes != version.size_bytes
            or expected_session_id is not None and item.session_id != expected_session_id):
            raise AuditIntegrityError("attachment inspection differs from its actual session/blob version")
    if response["success"]:
        data = response.get("data")
        if (type(data) is not dict or data.get("blob_id") != raw_blob_id
            or data.get("content_hash") != version.content_hash or data.get("size_bytes") != version.size_bytes):
            raise AuditIntegrityError("attachment response differs from its inspected original version")


def apply_retained_attachment_history(
    *, history: ContinuationAttachmentHistory, read_effect_key: str, result: ContinuationReadOwnerResult,
) -> ContinuationAttachmentHistory:
    from elspeth.web.sessions.continuation_owner_results import re_admit_read_owner_result
    if type(history) is not ContinuationAttachmentHistory:
        raise TypeError("read application requires the actual historical owner")
    if type(read_effect_key) is not str or not read_effect_key:
        raise AuditIntegrityError("attachment read application has no selected effect identity")
    re_admit_read_owner_result(result)
    require_read_attachment_history(result, expected_session_id=history.session_id)
    if not result.attachment_inspections:
        return history
    incoming = RetainedAttachmentRead(effect_key=read_effect_key, inspections=result.attachment_inspections)
    prior = tuple(item for item in history.reads if item.effect_key == read_effect_key)
    if prior:
        if prior != (incoming,):
            raise AuditIntegrityError("same selected read changed its complete attachment inspection history")
        return history
    return ContinuationAttachmentHistory(
        session_id=history.session_id, operation_id=history.operation_id, reads=(*history.reads, incoming),
    )


def _selected_chunk(reference: PrivateChunkRef, chunks: tuple[PrivateChunk, ...]) -> PrivateChunk:
    matches = tuple(chunk for chunk in chunks if chunk.reference == reference)
    if len(matches) != 1 or type(matches[0]) is not PrivateChunk:
        raise AuditIntegrityError("attachment application has no unique complete selected chunk")
    verify_private_chunk(reference, matches[0].canonical)
    return matches[0]


def retain_read_attachment_history(
    *, stack: ContinuationFrameStack, chunks: tuple[PrivateChunk, ...], effect_key: str,
    result: ContinuationReadOwnerResult,
) -> tuple[ContinuationFrameStack, tuple[PrivateChunk, ...]]:
    from elspeth.web.sessions.continuation_owner_results import decode_read_owner_result
    if type(stack) is not ContinuationFrameStack or type(chunks) is not tuple:
        raise TypeError("attachment application requires the selected complete continuation values")
    if any(type(chunk) is not PrivateChunk for chunk in chunks):
        raise TypeError("attachment application requires complete nominal private chunks")
    pending = tuple(item for item in stack.pending_owner_results if item.effect_key == effect_key)
    if len(pending) != 1 or pending[0].kind != "read":
        raise AuditIntegrityError("attachment application has no exact selected pending read")
    pending_chunk = _selected_chunk(pending[0].result, chunks)
    if decode_read_owner_result(pending_chunk.canonical) != result:
        raise AuditIntegrityError("attachment application differs from the selected pending read bytes")
    positions = tuple(index for index, frame in enumerate(stack.frames) if type(frame) is ComposeFrame)
    if len(positions) != 1:
        raise AuditIntegrityError("attachment application requires exactly one owning Compose frame")
    position = positions[0]
    frame = stack.frames[position]
    if type(frame) is not ComposeFrame:
        raise AuditIntegrityError("attachment application selected another frame kind")
    history = decode_attachment_history(
        _selected_chunk(frame.attachment_admissions, chunks),
        session_id=stack.logical.session_id, operation_id=stack.logical.operation_id,
    )
    following = apply_retained_attachment_history(history=history, read_effect_key=effect_key, result=result)
    if following == history:
        return stack, chunks
    encoded = encode_attachment_history(following)
    existing = tuple(chunk for chunk in chunks if chunk.reference == encoded.reference)
    if any(chunk.canonical != encoded.canonical for chunk in existing):
        raise AuditIntegrityError("attachment history digest refers to different complete bytes")
    next_frame = ComposeFrame.model_validate({**frame.model_dump(mode="python"), "attachment_admissions": encoded.reference})
    next_frames = (*stack.frames[:position], next_frame, *stack.frames[position + 1:])
    next_stack = ContinuationFrameStack.model_validate({**stack.model_dump(mode="python"), "frames": next_frames})
    return next_stack, chunks if existing else (*chunks, encoded)
```

Historical read/inspection values stay plain closed records. `require_read_attachment_history` does not call `inspect_attachment_original`, Prompt Shield, a provider or a current trust-materializer. `re_admit_read_owner_result` does current pure response-shape admission, not a content clearance. A current version mismatch still refuses in the SQL read owner before B5 sees these bytes. Old detector/policy values are valid historical facts; they never satisfy a later scan.

## A2: Capture the actual H3c receipt in the retained read owner

**Modify:** `src/elspeth/web/sessions/continuation_owner_results.py`, `src/elspeth/web/composer/tools/blobs.py`.

- [ ] Extend C3.8e's existing `ContinuationReadObservations` with the exact new slot/list/method/property below. Keep its existing `_versions`, `observe_blob` and `versions` bodies unchanged. Add normal imports for the two nominal types.

```python
from elspeth.web.composer.attachment_admission import AttachmentReadAdmission
from elspeth.web.composer.continuation_attachment_history import RetainedAttachmentInspection

# ContinuationReadObservations.__slots__ replaces its previous single tuple.
__slots__ = ("_versions", "_attachment_inspections")

# Append to its actual __init__ after self._versions is created.
self._attachment_inspections: list[RetainedAttachmentInspection] = []

# These are complete additional class members.
def observe_attachment_inspection(self, actual: AttachmentReadAdmission) -> None:
    self._attachment_inspections.append(RetainedAttachmentInspection.from_actual(actual))

@property
def attachment_inspections(self) -> tuple[RetainedAttachmentInspection, ...]:
    return tuple(self._attachment_inspections)
```

- [ ] Add `attachment_inspections: tuple[RetainedAttachmentInspection, ...] = ()` to `ContinuationReadOwnerResult`. Add the explicit constructor argument `attachment_inspections=observations.attachment_inspections` to `build_read_owner_result`. In `re_admit_read_owner_result`, immediately before its existing `return envelope`, insert the first block below. In `require_read_versions_on_connection`, immediately after its existing `re_admit_read_owner_result(result)`, insert the second block. Keep the actual SQL row checks, complete `tool_result_json` and selected response contract admission unchanged.

```python
from elspeth.web.composer.continuation_attachment_history import require_read_attachment_history
require_read_attachment_history(result)
```

```python
from elspeth.web.composer.continuation_attachment_history import require_read_attachment_history
require_read_attachment_history(result, expected_session_id=session_id)
```

The default `()` preserves current in-process construction of non-original-inspection read values. `decode_read_owner_result` already requires complete serialized-key equality, so an old stored text-read result missing this field is refused rather than promoted into an inspection. Newly admitted v2 results always serialize the field. No migration or retrospective inspection is implied.

- [ ] Add keyword-only `continuation_read_observations: ContinuationReadObservations | None = None` to H3c's `_locked_read_ready_blob`, after `attachment_inspection_policy`. C3.8e already supplies its concrete import in `blobs.py`. Replace H3c's discarded `inspect_attachment_original(...)` call with this exact block inside the existing ready-row custody lock, after actual hash and byte-length verification. Do not move a capture before the primitive returns.

```python
                admission = inspect_attachment_original(
                    session_id=session_id,
                    blob_id=blob_id,
                    verified_hash=verified_hash,
                    raw=data,
                    policy=attachment_inspection_policy,
                )
                if continuation_read_observations is not None:
                    continuation_read_observations.observe_blob(record)
                    continuation_read_observations.observe_attachment_inspection(admission)
```

- [ ] In H3c's actual `_read_admitted_attachment` call to `_locked_read_ready_blob`, add exactly:

```python
            continuation_read_observations=context.continuation_read_observations,
```

Use A2a's actual current policy construction; preserve `AttachmentInspectionRefused`, credential refusal and strict UTF-8 exception arms. Keep C3's existing later `observe_blob` call in both content readers: it confirms the same version and is idempotent. A successful full inspection followed by an expected-hash/cursor/response-size refusal can legitimately have inspection history on a failed tool result. A primitive credential/UTF-8/resource refusal has none. `inspect_source`'s bounded streaming read has blob-version provenance but does not call H3c's primitive and must not manufacture an inspection receipt.

## A2a: Bind every inspection policy to the actual current session actor

**Modify:** `src/elspeth/web/composer/tools/blobs.py`.
**Append tests:** A4's `tests/unit/web/composer/test_continuation_attachment_history.py`.
This is the narrow correction to H3c's original `AttachmentInspectionPolicy(max_bytes=...)`
construction. The actual `ToolContext` already carries the session engine,
context, authority, optional secret resolver and optional expected user. Read
the current SQL principal; never manufacture an actor or resolve secret values.
The current detector's additional-field inventory is
`DEFAULT_CREDENTIAL_TRAVERSAL_POLICY.additional_credential_fields`; WebSettings
has no separate additional-field setting at this source bind.

- [ ] Add these imports and complete helper to `blobs.py`, immediately before
  `_read_admitted_attachment`. Use its existing `select`/`Engine`/session imports
  where present. The helper's optional arguments match the actual `ToolContext`;
  missing live custody is an explicit integrity refusal.

```python
from elspeth.contracts.credential_material import DEFAULT_CREDENTIAL_TRAVERSAL_POLICY
from elspeth.contracts.secrets import WebSecretResolver
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.protocol import SessionOperationAuthority


def current_attachment_inspection_policy(
    *, session_engine: Engine, session_id: str,
    session_operation_context: SessionOperationContext | None,
    session_operation_authority: SessionOperationAuthority | None,
    secret_service: WebSecretResolver | None, expected_actor_user_id: str | None,
    max_bytes: int,
) -> AttachmentInspectionPolicy:
    operation = session_operation_context
    if (type(operation) is not SessionOperationContext or session_operation_authority is None
        or operation.operation_kind is not SessionOperationKind.COMPOSE
        or operation.fence.session_id != session_id):
        raise AuditIntegrityError("attachment inspection requires its actual current compose session")
    session_operation_authority.compare_and_swap(operation)
    with session_engine.connect() as conn:
        principal = conn.execute(select(sessions_table.c.user_id, sessions_table.c.archived_at).where(
            sessions_table.c.id == session_id)).one()
    if (principal.archived_at is not None or type(principal.user_id) is not str or not principal.user_id
        or expected_actor_user_id is not None and principal.user_id != expected_actor_user_id):
        raise AuditIntegrityError("attachment inspection differs from the actual current session principal")
    names = frozenset(item.name for item in secret_service.list_refs(principal.user_id)) if secret_service is not None else frozenset()
    policy = AttachmentInspectionPolicy(max_bytes=max_bytes, env_ref_names=names,
        additional_credential_fields=frozenset(DEFAULT_CREDENTIAL_TRAVERSAL_POLICY.additional_credential_fields))
    session_operation_authority.compare_and_swap(operation)
    return policy
```

- [ ] In H3c's `_read_admitted_attachment`, replace only its policy expression
  with the complete call below; retain A2's observation argument and existing
  exception arms. `_locked_read_ready_blob` independently enters present blob
  custody and rechecks the fence/hash/length before it inspects every original.

```python
attachment_inspection_policy=current_attachment_inspection_policy(
    session_engine=engine, session_id=session_id,
    session_operation_context=context.session_operation_context,
    session_operation_authority=context.session_operation_authority,
    secret_service=context.secret_service, expected_actor_user_id=context.user_id,
    max_bytes=context.attachment_inspection_max_bytes),
```

- [ ] Append these complete tests after A4's fixtures. They use actual SQLite
  admission and the real secret inventory. Only spies prevent accidental value
  resolution; the real `list_refs`, detector, reader and custody run unchanged.
  The detector treats attachment UTF-8 as text; unknown `${NAME}` is refused in
  a structured credential field, not asserted to be a secret in arbitrary prose.

```python
from elspeth.contracts.credential_material import DEFAULT_CREDENTIAL_TRAVERSAL_POLICY
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material
from elspeth.web.secrets.server_store import ServerSecretStore
from elspeth.web.secrets.service import ScopedSecretResolver, WebSecretService
from elspeth.web.secrets.user_store import UserSecretStore


@pytest.mark.asyncio
async def test_current_inspection_policy_uses_sql_actor_and_registered_names_only(
    operation_store, tmp_path, tool_context, monkeypatch,
):
    case = await _case(operation_store, tmp_path, tool_context)
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "attachment-fixture-fingerprint-key")
    store = UserSecretStore(case.engine, master_key="attachment-fixture-master-key-at-least-32-characters")
    store.set_secret("AZURE_API_KEY", value="not-projected-as-content", user_id="alice", auth_provider_type="local")
    resolver = ScopedSecretResolver(WebSecretService(store, ServerSecretStore(allowlist=())), "local")
    listed = []
    actual_list = resolver.list_refs
    def list_actual(actor):
        listed.append(actor)
        return actual_list(actor)
    def refuse_resolution(*args, **kwargs):
        raise AssertionError("inspection policy must not resolve credential values")
    monkeypatch.setattr(resolver, "list_refs", list_actual)
    monkeypatch.setattr(resolver, "resolve", refuse_resolution)
    monkeypatch.setattr(resolver, "resolve_scoped", refuse_resolution)
    try:
        policy = blobs.current_attachment_inspection_policy(session_engine=case.engine, session_id=str(case.sid),
            session_operation_context=case.running.session_operation_context,
            session_operation_authority=case.repository, secret_service=resolver,
            expected_actor_user_id=None, max_bytes=case.context.attachment_inspection_max_bytes)
        assert listed == ["alice"]
        assert policy.env_ref_names == frozenset({"AZURE_API_KEY"})
        assert policy.additional_credential_fields == frozenset(DEFAULT_CREDENTIAL_TRAVERSAL_POLICY.additional_credential_fields)
        require_no_credential_material({"api_key": "${AZURE_API_KEY}"}, surface="attachment-policy-test",
            env_ref_names=policy.env_ref_names, additional_credential_fields=policy.additional_credential_fields)
        for forbidden in ({"api_key": "${UNREGISTERED_KEY}"}, {"api_key": "literal-sensitive-value"}):
            with pytest.raises(CredentialMaterialRefused):
                require_no_credential_material(forbidden, surface="attachment-policy-test",
                    env_ref_names=policy.env_ref_names, additional_credential_fields=policy.additional_credential_fields)
        context = replace(case.context, secret_service=resolver, user_id="alice")
        observed_case = replace(case, context=context)
        with patch.object(blobs, "inspect_attachment_original", wraps=blobs.inspect_attachment_original) as scan:
            _selected, result = _retained_read(observed_case, effect_key="tool:actual-policy")
            assert scan.call_count == 1
            assert scan.call_args.kwargs["policy"] == policy
            assert result.attachment_inspections[0].policy_fingerprint == policy.fingerprint
        before = len(listed)
        with pytest.raises(AuditIntegrityError, match="principal"):
            blobs.current_attachment_inspection_policy(session_engine=case.engine, session_id=str(case.sid),
                session_operation_context=case.running.session_operation_context,
                session_operation_authority=case.repository, secret_service=resolver,
                expected_actor_user_id="another-user", max_bytes=policy.max_bytes)
        assert len(listed) == before
        plain = blobs.current_attachment_inspection_policy(session_engine=case.engine, session_id=str(case.sid),
            session_operation_context=case.running.session_operation_context,
            session_operation_authority=case.repository, secret_service=None,
            expected_actor_user_id="alice", max_bytes=policy.max_bytes)
        assert plain.env_ref_names == frozenset()
        require_no_credential_material({"api_key": {"secret_ref": "AZURE_API_KEY"}}, surface="attachment-policy-test",
            env_ref_names=plain.env_ref_names)
    finally:
        case.repository.release(case.running.session_operation_context)


@pytest.mark.asyncio
async def test_fresh_policy_refuses_released_fence_before_listing_names(operation_store, tmp_path, tool_context):
    case = await _case(operation_store, tmp_path, tool_context)
    case.repository.release(case.running.session_operation_context)
    from elspeth.web.coordination.contracts import SessionOperationFenceLost
    with pytest.raises(SessionOperationFenceLost):
        blobs.current_attachment_inspection_policy(session_engine=case.engine, session_id=str(case.sid),
            session_operation_context=case.running.session_operation_context,
            session_operation_authority=case.repository, secret_service=None,
            expected_actor_user_id="alice", max_bytes=case.context.attachment_inspection_max_bytes)


@pytest.mark.asyncio
async def test_actual_read_refuses_late_literal_credential_without_inspection_receipt(
    operation_store, tmp_path, tool_context,
):
    case = await _case(operation_store, tmp_path, tool_context)
    # Synthetic detector-shaped input, assembled so no repository credential
    # scanner can mistake a fixture for an actual issued token.
    unsafe = b"safe-prefix\n" * 2000 + ("sk-" + "A" * 48).encode("ascii")
    case = replace(case, context=replace(case.context, attachment_inspection_max_bytes=len(unsafe) + 1))
    case.path.write_bytes(unsafe)
    with case.engine.begin() as conn:
        conn.execute(update(blobs_table).where(blobs_table.c.id == case.blob_id).values(
            size_bytes=len(unsafe), content_hash=hashlib.sha256(unsafe).hexdigest()))
    try:
        with patch.object(blobs, "inspect_attachment_original", wraps=blobs.inspect_attachment_original) as scan:
            _selected, result = _retained_read(case, effect_key="tool:late-literal-refusal", tool_name="read_attachment")
        assert scan.call_count == 1 and scan.call_args.kwargs["raw"] == unsafe
        response = json.loads(result.response_json)
        assert response["success"] is False and "data" not in response
        assert result.attachment_inspections == ()
        assert response["validation"]["errors"][0]["error_code"] == "credential_material_rejected"
    finally:
        case.repository.release(case.running.session_operation_context)
```

- [ ] For A2a's behavioral RED, temporarily replace only `names = ...` in the
  helper with `names = frozenset()`. Run the two tests above; require native
  exit 1 at the exact registered-name positive assertion. Restore the actual
  inventory call and require native exit 0 for A4's whole module using A5's
  commands. A setup/import failure is not RED.

## A3: Bind history to bootstrap and the actual B5 application CAS

- [ ] In frames F14's `composer.continuation_driver.initialize_continuation_composition(*, service, operation_authority, selected: SelectedContinuationState, required_work, provider_owner, progress) -> SelectedContinuationState`, at `Initialize.ready -> ComposeFrame`, create the actual attachment chunk with this code. `root` is the decoded selected `ContinuationFrameStack` already used by this constructor, not a new input port. Include `attachments` in the exact same chunk tuple as the new frame and assign `attachment_admissions=attachments.reference`. This is the only initialization to empty history, before the Compose frame has retained reads. F14 owns that full constructor; the exact expression below is its attachment owner binding, not a second initializer.

```python
from elspeth.web.composer.continuation_attachment_history import empty_attachment_history, encode_attachment_history

attachments = encode_attachment_history(empty_attachment_history(
    session_id=root.logical.session_id, operation_id=root.logical.operation_id,
))
```

- [ ] In F9's complete `capture_continuation_batch_owners` in `continuation_execution.py`, immediately after the existing exact `ComposeFrame` check and before `prior_iteration = ...`, insert this complete call. F14's runtime restore applies the same decode to its selected Compose frame before entering business phases. Preserve the existing `attachment_admissions` reference in every subsequent frame capture. Do not initialize it again, derive it from a current page, or scan historical bytes to retroactively manufacture this history.

```python
from elspeth.web.composer.continuation_attachment_history import decode_attachment_history

decode_attachment_history(
    continuation_chunk(chunks, frame.attachment_admissions),
    session_id=stack.logical.session_id, operation_id=stack.logical.operation_id,
)
```

The decoded value is historical data only. The actual fresh blob read still enters `_read_admitted_attachment`; T8's current replay use enters the same locked reader with A2a's current policy. Neither receives this value as clearance. F9's `continuation_chunk` is its already defined exact ref/digest reader; the code above adds no callback or new SQL owner.

- [ ] Preserve F7's already defined complete `pin_evidence` attachment member below and its two normal imports. This is the literal dictionary member inside `chunks`; it is already integrated in the frames packet and needs no second edit. No opaque attachment reference is permitted.

```python-mapping-member
"attachments": encode_attachment_history(empty_attachment_history(
    session_id=root.logical.session_id, operation_id=root.logical.operation_id,
)),
```

- [ ] Preserve B5's now literal read branch in `resume_continuation_tool_batch`, after `_execute_continuation_call_live` has actually applied the selected result and before `batch.next_tool_index += 1`. This is its complete history branch, using the actual `ContinuationBatchExecution` fields. B5's existing actual tool result/accumulator application remains the owner of provider-visible results.

```python
from elspeth.web.composer.continuation_attachment_history import retain_read_attachment_history
from elspeth.web.sessions.continuation_owner_results import decode_read_owner_result

if call.owner_result_kind == "read":
    if call.owner_result_json is None or call.effect_key is None:
        raise AuditIntegrityError("read application lost its actual retained owner result")
    execution.stack, execution.chunks = retain_read_attachment_history(
        stack=execution.stack, chunks=execution.chunks, effect_key=call.effect_key,
        result=decode_read_owner_result(call.owner_result_json),
    )
```

The following `commit_owner_application` must receive this returned stack/chunk tuple and remove only the selected pending read under its already defined `expected_checkpoint_id`/`expected_cursor` CAS. No intermediate `commit_transition` is added. Therefore a crash before CAS retains a pending read and unchanged history; after CAS it retains appended history and a consumed pending read. Neither side dispatches the handler again to repair bookkeeping. A1 itself leaves cursor, pending entries, effect frontier, semantic frontier and other frames unchanged.

## A4: Actual-read, actual-store and fault tests

**Create:** `tests/unit/web/composer/test_continuation_attachment_history.py`.

- [ ] Add this complete module. Its fixture enters F7's real admission, ingress, evidence and selected-root writers. It calls the actual locked content reader and actual retained-read SQL owner. Its helper `_apply` isolates only the pure historical projection, then uses the actual B5 SQL CAS; it does not claim to test provider-visible tool delivery or .10 current materialization. No scan, final Composer result, read handler or SQL writer is mocked. The observer spy calls the real primitive and counts its actual invocations.

```python
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, event, insert, select, update
from sqlalchemy.sql.dml import Update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_attachment_history import (
    RetainedAttachmentInspection,
    apply_retained_attachment_history,
    decode_attachment_history,
    empty_attachment_history,
    encode_attachment_history,
    retain_read_attachment_history,
)
from elspeth.web.composer.continuation_codec import (
    PrivateChunk, decode_continuation_snapshot, encode_continuation_snapshot, encode_private_chunk,
)
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import blobs
from elspeth.web.composer.tools._common import ToolContext
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import ContinuationCASConflict, ContinuationOwnerCommit, prepare_effect_on_connection
from elspeth.web.sessions.continuation_owner_results import (
    ContinuationReadObservations, ContinuationReadOwnerResult, build_read_owner_result, decode_read_owner_result,
)
from elspeth.web.sessions.models import (
    blobs_table, composer_async_operations_table as jobs, composer_continuation_chunks_table as chunks_table,
)
from tests.unit.web.composer.conftest import tool_context as tool_context
from tests.unit.web.composer.test_continuation_evidence import new_evidence, pin_evidence
from tests.unit.web.sessions.test_continuation_cold_restore import operation_store as operation_store


@dataclass(frozen=True, slots=True)
class AttachmentCase:
    engine: Engine
    repository: SQLiteLocalSessionOperationAuthority
    authority: ComposerAsyncOperationAuthority
    running: ComposerOperationRunning
    sid: UUID
    state: CompositionState
    context: ToolContext
    blob_id: str
    path: Path
    body: bytes


async def _case(store, tmp_path, tool_context):
    engine, repository, authority, _service, sid = store
    _record, _seed, _action, running, owner, messages = await new_evidence(store)
    pin_evidence(store, running, owner, messages)
    body = b"customer_id,amount\nA,7\n"
    blob_id = str(uuid4())
    path = tmp_path / "blobs" / str(sid) / f"{blob_id}_uploaded.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    with engine.begin() as conn:
        conn.execute(insert(blobs_table).values(
            id=blob_id, session_id=str(sid), filename="uploaded.csv", mime_type="text/csv",
            size_bytes=len(body), content_hash=hashlib.sha256(body).hexdigest(), storage_path=str(path),
            created_at=datetime.now(UTC), created_by="user", creation_modality="verbatim", status="ready",
        ))
    context = replace(
        tool_context, session_engine=engine, session_id=str(sid), data_dir=str(tmp_path),
        session_operation_context=running.session_operation_context, session_operation_authority=repository,
    )
    return AttachmentCase(engine, repository, authority, running, sid, owner.effects.latest_verified_state,
                          context, blob_id, path, body)


def _retained_read(case, *, effect_key, tool_name="get_blob_content", extra_arguments=None):
    arguments = {"blob_id": case.blob_id, **(extra_arguments or {})}
    selected = case.authority.load_continuation_snapshot(case.running)
    digest = hashlib.sha256(canonical_json({"tool": tool_name, "arguments": arguments}).encode("utf-8")).hexdigest()
    with case.engine.begin() as conn:
        prepared = prepare_effect_on_connection(
            conn, case.running, effect_key=effect_key, input_sha256=digest, owner="read",
            next_snapshot_json=selected.payload_json, expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
        )
    observations = ContinuationReadObservations()
    context = replace(case.context, continuation_read_observations=observations)
    handler = {"get_blob_content": blobs._execute_get_blob_content,
               "read_attachment": blobs._execute_read_attachment,
               "get_blob_metadata": blobs._handle_get_blob_metadata}[tool_name]
    actual = handler(arguments, case.state, context)
    value = build_read_owner_result(
        tool_name=tool_name, arguments=arguments, result=actual, pre_state=case.state, observations=observations,
    )
    commit = ContinuationOwnerCommit(case.running, effect_key, digest, prepared.checkpoint_id, prepared.cursor)
    retained = case.authority.retain_read_owner_result(commit, result=value)
    return retained, value


def _history(case, stored):
    root = decode_continuation_snapshot(stored.payload_json)
    selected = case.authority.load_continuation_chunks(case.running, stored)
    frames = tuple(frame for frame in root.frames if type(frame) is ComposeFrame)
    assert len(frames) == 1
    found = tuple(chunk for chunk in selected if chunk.reference == frames[0].attachment_admissions)
    assert len(found) == 1
    return decode_attachment_history(found[0], session_id=str(case.sid), operation_id=case.running.claim.operation_id)


def _candidate(case, retained, effect_key):
    kind, encoded = case.authority.load_pending_owner_result(
        case.running, expected_checkpoint_id=retained.checkpoint_id, expected_cursor=retained.cursor,
        effect_key=effect_key,
    )
    assert kind == "read"
    result = decode_read_owner_result(encoded)
    root = decode_continuation_snapshot(retained.payload_json)
    selected = case.authority.load_continuation_chunks(case.running, retained)
    following, values = retain_read_attachment_history(stack=root, chunks=selected, effect_key=effect_key, result=result)
    assert following.cursor == root.cursor
    assert following.pending_owner_results == root.pending_owner_results
    assert following.effect_frontier == root.effect_frontier
    assert following.semantic_frontier == root.semantic_frontier
    assert following.active_action_json == root.active_action_json
    final = following.model_copy(update={
        "pending_owner_results": tuple(item for item in following.pending_owner_results if item.effect_key != effect_key),
    })
    return final, values


def _commit(case, retained, effect_key, following, values):
    return case.authority.commit_owner_application(
        case.running, expected_checkpoint_id=retained.checkpoint_id, expected_cursor=retained.cursor,
        snapshot_json=encode_continuation_snapshot(following), chunks=values, effect_key=effect_key,
    )


def _apply(case, retained, effect_key):
    following, values = _candidate(case, retained, effect_key)
    return _commit(case, retained, effect_key, following, values)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ("get_blob_content", "read_attachment"))
async def test_actual_inspection_is_retained_and_applied_from_selected_sql_without_rescan(
    operation_store, tmp_path, tool_context, tool_name,
):
    case = await _case(operation_store, tmp_path, tool_context)
    try:
        with patch.object(blobs, "inspect_attachment_original", wraps=blobs.inspect_attachment_original) as scan:
            retained, actual = _retained_read(case, effect_key="tool:attachment:1", tool_name=tool_name)
            assert scan.call_count == 1
            assert len(actual.attachment_inspections) == 1
            observed = actual.attachment_inspections[0]
            assert observed.session_id == str(case.sid) and observed.blob_id == case.blob_id
            assert observed.content_hash == hashlib.sha256(case.body).hexdigest()
            assert observed.size_bytes == len(case.body)
            assert observed.detector_version and len(observed.policy_fingerprint) == 64
            assert _history(case, retained).reads == ()
            del actual
            applied = _apply(case, retained, "tool:attachment:1")
            restored = _history(case, applied)
            assert scan.call_count == 1
            assert restored.reads[0].effect_key == "tool:attachment:1"
            assert restored.reads[0].inspections == (observed,)
            assert not decode_continuation_snapshot(applied.payload_json).pending_owner_results
            assert decode_attachment_history(
                encode_attachment_history(restored), session_id=str(case.sid), operation_id=case.running.claim.operation_id,
            ) == restored
            assert scan.call_count == 1
            with pytest.raises(ContinuationCASConflict):
                _apply(case, retained, "tool:attachment:1")
            assert case.authority.load_continuation_snapshot(case.running) == applied
    finally:
        case.repository.release(case.running.session_operation_context)


@pytest.mark.asyncio
async def test_prior_history_never_skips_fresh_inspection_and_distinct_reads_keep_distinct_history(
    operation_store, tmp_path, tool_context,
):
    case = await _case(operation_store, tmp_path, tool_context)
    try:
        with patch.object(blobs, "inspect_attachment_original", wraps=blobs.inspect_attachment_original) as scan:
            first, _value = _retained_read(case, effect_key="tool:attachment:first")
            first_applied = _apply(case, first, "tool:attachment:first")
            assert len(_history(case, first_applied).reads) == 1
            second, _value = _retained_read(case, effect_key="tool:attachment:second")
            second_applied = _apply(case, second, "tool:attachment:second")
            assert scan.call_count == 2
            restored = _history(case, second_applied)
            assert [read.effect_key for read in restored.reads] == ["tool:attachment:first", "tool:attachment:second"]
            assert restored.reads[0].inspections == restored.reads[1].inspections
        # New actual bytes are freshly inspected, even though the selected
        # root still contains the complete successful history above.
        unsafe = b"neutral\n" + b"sk-" + b"A" * 48
        case.path.write_bytes(unsafe)
        with case.engine.begin() as conn:
            conn.execute(update(blobs_table).where(blobs_table.c.id == case.blob_id).values(
                size_bytes=len(unsafe), content_hash=hashlib.sha256(unsafe).hexdigest(),
            ))
        observations = ContinuationReadObservations()
        actual = blobs._execute_get_blob_content({"blob_id": case.blob_id}, case.state,
            replace(case.context, continuation_read_observations=observations))
        assert not actual.success and actual.data is None
        assert observations.attachment_inspections == ()
        assert len(_history(case, second_applied).reads) == 2
    finally:
        case.repository.release(case.running.session_operation_context)


@pytest.mark.asyncio
async def test_history_cas_failure_rolls_back_history_and_pending_consumption_together(
    operation_store, tmp_path, tool_context,
):
    case = await _case(operation_store, tmp_path, tool_context)
    try:
        retained, _value = _retained_read(case, effect_key="tool:attachment:atomic")
        following, values = _candidate(case, retained, "tool:attachment:atomic")
        assert case.authority.load_continuation_snapshot(case.running) == retained
        old_frame = decode_continuation_snapshot(retained.payload_json).frames[0]
        assert type(old_frame) is ComposeFrame
        next_frame = following.frames[0]
        assert type(next_frame) is ComposeFrame
        assert old_frame.attachment_admissions != next_frame.attachment_admissions
        inserted_key = f"value:composer.attachment-admissions.v1:{next_frame.attachment_admissions.digest}"
        fault = RuntimeError("attachment-application-rollback")
        triggered = []

        def fail_after_actual_pointer_update(conn, cursor, statement, parameters, context, executemany):
            compiled = context.compiled
            if compiled is not None and isinstance(compiled.statement, Update) and compiled.statement.table is jobs:
                triggered.append(True)
                raise fault

        event.listen(case.engine, "after_cursor_execute", fail_after_actual_pointer_update)
        try:
            with pytest.raises(RuntimeError) as raised:
                _commit(case, retained, "tool:attachment:atomic", following, values)
            assert raised.value is fault and triggered == [True]
        finally:
            event.remove(case.engine, "after_cursor_execute", fail_after_actual_pointer_update)
        assert case.authority.load_continuation_snapshot(case.running) == retained
        assert _history(case, retained).reads == ()
        assert len(decode_continuation_snapshot(retained.payload_json).pending_owner_results) == 1
        with case.engine.connect() as conn:
            assert conn.execute(select(chunks_table).where(
                chunks_table.c.session_id == str(case.sid), chunks_table.c.operation_id == case.running.claim.operation_id,
                chunks_table.c.chunk_key == inserted_key,
            )).first() is None
        applied = _commit(case, retained, "tool:attachment:atomic", following, values)
        assert [read.effect_key for read in _history(case, applied).reads] == ["tool:attachment:atomic"]
        assert not decode_continuation_snapshot(applied.payload_json).pending_owner_results
    finally:
        case.repository.release(case.running.session_operation_context)


@pytest.mark.asyncio
async def test_complete_history_codec_and_same_effect_collision_refuse_without_mutation(
    operation_store, tmp_path, tool_context,
):
    case = await _case(operation_store, tmp_path, tool_context)
    try:
        retained, value = _retained_read(case, effect_key="tool:attachment:codec")
        history = apply_retained_attachment_history(
            history=empty_attachment_history(session_id=str(case.sid), operation_id=case.running.claim.operation_id),
            read_effect_key="tool:attachment:codec", result=value,
        )
        assert apply_retained_attachment_history(history=history, read_effect_key="tool:attachment:codec", result=value) is history
        chunk = encode_attachment_history(history)
        for missing in ("schema_version", "session_id", "operation_id", "reads"):
            payload = json.loads(chunk.canonical)
            del payload[missing]
            bad = encode_private_chunk(codec="composer.attachment-admissions.v1", canonical=canonical_json(payload))
            with pytest.raises(AuditIntegrityError):
                decode_attachment_history(bad, session_id=str(case.sid), operation_id=case.running.claim.operation_id)
        for field in ("session_id", "blob_id", "content_hash", "size_bytes", "detector_version", "policy_fingerprint"):
            payload = json.loads(chunk.canonical)
            del payload["reads"][0]["inspections"][0][field]
            bad = encode_private_chunk(codec="composer.attachment-admissions.v1", canonical=canonical_json(payload))
            with pytest.raises(AuditIntegrityError):
                decode_attachment_history(bad, session_id=str(case.sid), operation_id=case.running.claim.operation_id)
        payload = json.loads(chunk.canonical)
        payload["clearance"] = True
        with pytest.raises(AuditIntegrityError):
            decode_attachment_history(encode_private_chunk(codec="composer.attachment-admissions.v1", canonical=canonical_json(payload)),
                session_id=str(case.sid), operation_id=case.running.claim.operation_id)
        with pytest.raises(AuditIntegrityError, match="binding"):
            decode_attachment_history(PrivateChunk(chunk.reference, chunk.canonical.replace(case.blob_id, str(uuid4()))),
                session_id=str(case.sid), operation_id=case.running.claim.operation_id)
        with pytest.raises(AuditIntegrityError, match="logical request"):
            decode_attachment_history(chunk, session_id=str(case.sid), operation_id=str(uuid4()))
        # Historical policies may differ; replay under the SAME read identity
        # may not silently replace what that read actually retained.
        forged = json.loads(canonical_json(value.model_dump(mode="json")))
        forged["attachment_inspections"][0]["policy_fingerprint"] = "f" * 64
        changed = ContinuationReadOwnerResult.model_validate_json(canonical_json(forged), strict=True)
        with pytest.raises(AuditIntegrityError, match="same selected read"):
            apply_retained_attachment_history(history=history, read_effect_key="tool:attachment:codec", result=changed)
        root = decode_continuation_snapshot(retained.payload_json)
        selected = case.authority.load_continuation_chunks(case.running, retained)
        with pytest.raises(AuditIntegrityError, match="selected pending read bytes"):
            retain_read_attachment_history(stack=root, chunks=selected, effect_key="tool:attachment:codec", result=changed)
        assert case.authority.load_continuation_snapshot(case.running) == retained
    finally:
        case.repository.release(case.running.session_operation_context)


@pytest.mark.asyncio
async def test_read_result_must_bind_actual_session_version_and_complete_inspection(
    operation_store, tmp_path, tool_context,
):
    from elspeth.web.composer.continuation_attachment_history import require_read_attachment_history
    case = await _case(operation_store, tmp_path, tool_context)
    try:
        retained, value = _retained_read(case, effect_key="tool:attachment:bindings")
        for key, replacement in (("session_id", str(uuid4())), ("blob_id", str(uuid4())),
                                 ("content_hash", "0" * 64), ("size_bytes", len(case.body) + 1)):
            payload = json.loads(canonical_json(value.model_dump(mode="json")))
            payload["attachment_inspections"][0][key] = replacement
            changed = ContinuationReadOwnerResult.model_validate_json(canonical_json(payload), strict=True)
            with pytest.raises(AuditIntegrityError, match="session/blob version"):
                require_read_attachment_history(changed, expected_session_id=str(case.sid))
        payload = json.loads(canonical_json(value.model_dump(mode="json")))
        payload["attachment_inspections"] = []
        with pytest.raises(AuditIntegrityError, match="omitted"):
            decode_read_owner_result(canonical_json(payload))
        payload = json.loads(canonical_json(value.model_dump(mode="json")))
        del payload["attachment_inspections"]
        with pytest.raises(AuditIntegrityError, match="lost fields"):
            decode_read_owner_result(canonical_json(payload))
        with pytest.raises(TypeError, match="actual complete-original receipt"):
            RetainedAttachmentInspection.from_actual(value.attachment_inspections[0])
        with case.engine.begin() as conn:
            conn.execute(update(blobs_table).where(blobs_table.c.id == case.blob_id).values(size_bytes=len(case.body) + 1))
        try:
            with pytest.raises(AuditIntegrityError, match="immutable version"):
                case.authority.load_pending_owner_result(case.running,
                    expected_checkpoint_id=retained.checkpoint_id, expected_cursor=retained.cursor,
                    effect_key="tool:attachment:bindings")
        finally:
            with case.engine.begin() as conn:
                conn.execute(update(blobs_table).where(blobs_table.c.id == case.blob_id).values(size_bytes=len(case.body)))
        assert case.authority.load_continuation_snapshot(case.running) == retained
        assert _history(case, retained).reads == ()
    finally:
        case.repository.release(case.running.session_operation_context)


@pytest.mark.asyncio
async def test_metadata_is_not_an_inspection_and_post_inspection_tool_refusal_keeps_actual_history(
    operation_store, tmp_path, tool_context,
):
    case = await _case(operation_store, tmp_path, tool_context)
    try:
        with patch.object(blobs, "inspect_attachment_original", wraps=blobs.inspect_attachment_original) as scan:
            retained, metadata = _retained_read(case, effect_key="tool:metadata", tool_name="get_blob_metadata")
            assert metadata.attachment_inspections == () and scan.call_count == 0
            applied = _apply(case, retained, "tool:metadata")
            assert _history(case, applied).reads == ()
            refused, value = _retained_read(case, effect_key="tool:wrong-expected-hash",
                extra_arguments={"expected_content_hash": "0" * 64})
            envelope = json.loads(value.response_json)
            assert envelope["success"] is False and "data" not in envelope
            assert len(value.attachment_inspections) == 1 and scan.call_count == 1
            applied = _apply(case, refused, "tool:wrong-expected-hash")
            assert [read.effect_key for read in _history(case, applied).reads] == ["tool:wrong-expected-hash"]
    finally:
        case.repository.release(case.running.session_operation_context)
```

This fixture deliberately uses unprescribed ordinary read effects (`action_owner=None`), as existing C3.8e does. Prescribed action/debt and whole driver behavior remain covered by their real semantic/driver packets; these tests do not manufacture a semantic action solely to exercise attachment bookkeeping.

- [ ] Append these two complete tests to that same module. The first proves old policy observations remain historical values after the current policy changes. The second separates failure before a successful primitive return from failure after successful inspection; only the latter can have an inspection record.

```python
@pytest.mark.asyncio
async def test_changed_current_policy_keeps_old_history_and_scans_again(operation_store, tmp_path, tool_context):
    case = await _case(operation_store, tmp_path, tool_context)
    try:
        with patch.object(blobs, "inspect_attachment_original", wraps=blobs.inspect_attachment_original) as scan:
            first, old_result = _retained_read(case, effect_key="tool:policy:old")
            _apply(case, first, "tool:policy:old")
            next_context = replace(case.context, attachment_inspection_max_bytes=case.context.attachment_inspection_max_bytes + 1)
            next_case = replace(case, context=next_context)
            second, new_result = _retained_read(next_case, effect_key="tool:policy:new")
            applied = _apply(next_case, second, "tool:policy:new")
            history = _history(next_case, applied)
            assert scan.call_count == 2
            assert old_result.attachment_inspections[0].policy_fingerprint != new_result.attachment_inspections[0].policy_fingerprint
            assert history.reads[0].inspections == old_result.attachment_inspections
            assert history.reads[1].inspections == new_result.attachment_inspections
    finally:
        case.repository.release(case.running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ("size", "utf8"))
async def test_refused_original_has_no_successful_inspection_history(operation_store, tmp_path, tool_context, refusal):
    case = await _case(operation_store, tmp_path, tool_context)
    try:
        if refusal == "size":
            case = replace(case, context=replace(case.context, attachment_inspection_max_bytes=len(case.body) - 1))
        else:
            malformed = b"\xff"
            case.path.write_bytes(malformed)
            with case.engine.begin() as conn:
                conn.execute(update(blobs_table).where(blobs_table.c.id == case.blob_id).values(
                    size_bytes=len(malformed), content_hash=hashlib.sha256(malformed).hexdigest(),
                ))
        retained, value = _retained_read(case, effect_key="tool:original:refused")
        assert json.loads(value.response_json)["success"] is False
        assert value.attachment_inspections == ()
        applied = _apply(case, retained, "tool:original:refused")
        assert _history(case, applied).reads == ()
    finally:
        case.repository.release(case.running.session_operation_context)
```

- [ ] Replace the old `test_attachment_codec_is_stored_and_read_exactly` body in C4b's SQLite module with this complete test. Put the same complete test in the existing planned PostgreSQL module, using its actual `operation_store` and `admit_continuation` imports. This is a real storage test of the exact closed history, not a claim that an arbitrary old `{"files":[]}` value is a valid attachment history.

```python
def test_attachment_codec_is_stored_and_read_exactly(operation_store):
    from sqlalchemy import select
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.composer.continuation_attachment_history import (
        decode_attachment_history, empty_attachment_history, encode_attachment_history,
    )
    from elspeth.web.composer.continuation_codec import PrivateChunk, encode_private_chunk
    from elspeth.web.sessions.continuation import put_chunk, read_chunk
    from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
    engine, _repository, _authority, _service, sid = operation_store
    record, _seed, _action = admit_continuation(operation_store)
    history = empty_attachment_history(session_id=str(sid), operation_id=record.operation_id)
    encoded = encode_attachment_history(history)
    expected = encoded.canonical.encode("utf-8")
    with engine.begin() as conn:
        put_chunk(conn, session_id=str(sid), operation_id=record.operation_id,
                  key="attachment-admission-exact", payload=expected, codec=encoded.reference.codec)
        stored = conn.execute(select(chunks.c.codec, chunks.c.payload).where(
            chunks.c.operation_id == record.operation_id, chunks.c.chunk_key == "attachment-admission-exact",
        )).one()
        assert stored.codec == "composer.attachment-admissions.v1"
        assert bytes(stored.payload) == expected
        loaded = read_chunk(conn, session_id=str(sid), operation_id=record.operation_id, key="attachment-admission-exact")
        assert loaded == expected
    assert decode_attachment_history(PrivateChunk(encoded.reference, loaded.decode("utf-8")),
        session_id=str(sid), operation_id=record.operation_id) == history
    opaque = encode_private_chunk(codec="composer.attachment-admissions.v1", canonical='{"files":[]}')
    with pytest.raises(AuditIntegrityError, match="owned codec"):
        decode_attachment_history(opaque, session_id=str(sid), operation_id=record.operation_id)
    with engine.connect() as conn:
        assert read_chunk(conn, session_id=str(sid), operation_id=record.operation_id,
                          key="attachment-admission-exact") == expected
```

## A5: RED, GREEN, native checks and ordinary commits

- [ ] In the isolated implementation checkout only, set the lane-private command environment. The explicit import-path assertion is a prerequisite for reporting a test result from this worktree; it is not run during plan authoring.

```bash
cd "$COMPOSER_WORKTREE" &&
COMPOSER_WORKTREE="$(git rev-parse --show-toplevel)" &&
COMPOSER_PYTHON="$COMPOSER_WORKTREE/.venv/bin/python" &&
COMPOSER_LOG_DIR="$COMPOSER_WORKTREE/.claude/lanes/composer-continuation-implementation/attachment-history" &&
mkdir -p "$COMPOSER_LOG_DIR/tmp" &&
export TMPDIR="$COMPOSER_LOG_DIR/tmp" &&
export PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src" &&
"$COMPOSER_PYTHON" -c 'from pathlib import Path; import elspeth, elspeth_lints; root=Path.cwd(); assert Path(elspeth.__file__).is_relative_to(root); assert Path(elspeth_lints.__file__).is_relative_to(root); print(elspeth.__file__); print(elspeth_lints.__file__)'
```

- [ ] With A0's prerequisite owners and A1's importable contracts present, add A4's tests. Leave the observation capture unwired at A2 and run the actual-inspection behavioral RED below: the read succeeds but its retained inspection tuple is empty. Require that assertion failure and native nonzero exit. Missing modules, fixtures or configuration are setup failures.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest tests/unit/web/composer/test_continuation_attachment_history.py -k actual_inspection_is_retained -n 0 -v > "$COMPOSER_LOG_DIR/red-attachment-history.log" 2>&1
composer_attachment_exit=$?
cat "$COMPOSER_LOG_DIR/red-attachment-history.log"
printf 'exit=%s\n' "$composer_attachment_exit"
test "$composer_attachment_exit" -ne 0
```

- [ ] Implement A1–A3, then run the actual focused GREEN packet. Expected native exit 0. H3c's existing primitive tests retain the full-original late/page-seam credential, UTF-8, resource-policy and two-real-scan controls; C3's actual read-owner test retains immutable same-session versions. None is replaced by a regex over source.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest tests/unit/web/composer/test_continuation_attachment_history.py tests/unit/web/composer/test_attachment_admission.py tests/unit/web/composer/test_tools_blobs.py tests/unit/web/composer/test_continuation_evidence.py tests/unit/web/sessions/test_continuation_cold_restore.py -n 0 -v > "$COMPOSER_LOG_DIR/green-attachment-history.log" 2>&1
composer_attachment_exit=$?
cat "$COMPOSER_LOG_DIR/green-attachment-history.log"
printf 'exit=%s\n' "$composer_attachment_exit"
test "$composer_attachment_exit" -eq 0
```

- [ ] Temporarily remove only `attachment_inspections=observations.attachment_inspections` from `build_read_owner_result` in the owned implementation checkout. Re-run the actual-inspection test below. Expected native nonzero because an actual successful original read has lost its observation; no `{}` or default empty tuple may rescue it. Restore that exact constructor argument, then require the same command native exit 0. Do not retain the mutation or use a test skip.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest tests/unit/web/composer/test_continuation_attachment_history.py -k actual_inspection_is_retained -n 0 -v > "$COMPOSER_LOG_DIR/control-inspection-omitted.log" 2>&1
composer_attachment_exit=$?
cat "$COMPOSER_LOG_DIR/control-inspection-omitted.log"
printf 'exit=%s\n' "$composer_attachment_exit"
test "$composer_attachment_exit" -ne 0
```

- [ ] After restoring the constructor, run current whole-tree gates whose scanned inputs include these additions and the scoped Ruff check. The same normal affected-gate ownership rules apply; no extra controller/grant or broad suite reservation is created by this packet.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest tests/unit/web/test_sessions_composer_attribute_contracts.py tests/unit/elspeth_lints/test_masquerade_gate.py tests/unit/scripts/test_check_contracts.py -n 0 -v > "$COMPOSER_LOG_DIR/attachment-whole-tree-gates.log" 2>&1
composer_attachment_exit=$?
cat "$COMPOSER_LOG_DIR/attachment-whole-tree-gates.log"
printf 'exit=%s\n' "$composer_attachment_exit"
test "$composer_attachment_exit" -eq 0
```

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m ruff check src/elspeth/web/composer/continuation_attachment_history.py src/elspeth/web/sessions/continuation_owner_results.py src/elspeth/web/composer/tools/blobs.py src/elspeth/web/composer/continuation_tool_batch.py src/elspeth/web/composer/continuation_driver.py src/elspeth/web/composer/continuation_execution.py tests/unit/web/composer/test_continuation_attachment_history.py tests/unit/web/composer/test_continuation_evidence.py tests/unit/web/sessions/test_continuation_cold_restore.py tests/testcontainer/web/test_composer_continuation_postgres.py > "$COMPOSER_LOG_DIR/attachment-ruff.log" 2>&1
composer_attachment_exit=$?
cat "$COMPOSER_LOG_DIR/attachment-ruff.log"
printf 'exit=%s\n' "$composer_attachment_exit"
test "$composer_attachment_exit" -eq 0
```

- [ ] Run PostgreSQL's actual continuation module serially when the normal shared-capacity owner admits the implementation gate. This packet changes persisted private values and a SQL projection, so SQLite alone is insufficient. Root Q still owns full normal Python/Testcontainer integration qualification.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -m testcontainer tests/testcontainer/web/test_composer_continuation_postgres.py -n 0 -v > "$COMPOSER_LOG_DIR/attachment-postgres.log" 2>&1
composer_attachment_exit=$?
cat "$COMPOSER_LOG_DIR/attachment-postgres.log"
printf 'exit=%s\n' "$composer_attachment_exit"
test "$composer_attachment_exit" -eq 0
```

- [ ] Commit this bounded history owner together with its actual tests after GREEN. Inspect the staged set and run the actual branch-safety preflight before the ordinary commit. The larger implementation plan has separate commits for H3c, C3 and B5; splitting this narrow owner from its SQL tests would leave an unnecessarily incomplete test packet. These are future implementation commands, not authority to stage this ignored planning fragment.

```bash
cd "$COMPOSER_WORKTREE" && git add src/elspeth/web/composer/continuation_attachment_history.py src/elspeth/web/sessions/continuation_owner_results.py src/elspeth/web/composer/tools/blobs.py src/elspeth/web/composer/continuation_tool_batch.py src/elspeth/web/composer/continuation_driver.py src/elspeth/web/composer/continuation_execution.py tests/unit/web/composer/test_continuation_attachment_history.py tests/unit/web/composer/test_continuation_evidence.py tests/unit/web/sessions/test_continuation_cold_restore.py tests/testcontainer/web/test_composer_continuation_postgres.py
cd "$COMPOSER_WORKTREE" && git status --short
cd "$COMPOSER_WORKTREE" && git diff --cached --name-only
cd "$COMPOSER_WORKTREE" && scripts/branch-safety-check.sh --intent commit
cd "$COMPOSER_WORKTREE" && git commit -m "feat: apply attachment history with retained read checkpoints"
```

## A6: Boundaries for integration

- [ ] Core incorporates A1/A2's exact DTO and reader capture plus B5's already matching pure hook. Frames incorporates A3's initial closed empty history and preserves it in F9 capture/restore. Parent links this fragment or its durable assembled section from C1a/C3/B5 and Q. These ordinary cross-references are not an approval chain.
- [ ] Integrate [producers T6–T10](2026-10-21-composer-continuation-producers.md#t6-define-the-closed-native-and-attachment-replay-producers) with A2a. Its exact selected SQL source projection, complete current producer comparison and provider-bound required fresh read replace the former unspecified .10 materializer dependency. Old inspection facts remain historical; the gateway still performs its current full credential/Prompt Shield work on every actual SDK use.
- [ ] Report actual affected/whole-tree/PostgreSQL native exits when executed. Planning-only AST parsing or source review does not establish GREEN. Fresh-process/cold full-driver behavior remains the driver packet's responsibility; A4 establishes exact read/SQL/history behavior and fault atomicity only.
