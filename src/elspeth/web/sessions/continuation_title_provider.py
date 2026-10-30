"""Closed TITLE response retention and historical audit-source readmission.

Only the original audit writer may settle accounting. Historical coordinates
describe the retained payload; every writer still uses its current live fence.
No title mutation, ready transition or SDK retry is provided by this owner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, ValidationError
from sqlalchemy import Connection, Row, insert, select, update

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.composer.continuation import ContinuationValue, InitializeFrame, PrivateChunkRef
from elspeth.web.composer.continuation_bootstrap import bootstrap_values, decode_continuation_bootstrap
from elspeth.web.composer.continuation_clock import ContinuationActionClock, ContinuationTimingObservation, freeze_observation
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
    encode_private_chunk,
    verify_private_chunk,
)
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory, require_current_env_inventory_on_connection
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_provider_input import continuation_provider_input_hash, continuation_provider_input_json
from elspeth.web.composer.continuation_title_values import (
    CapturedTitleProviderResult,
    DecodedTitleProviderResult,
    decode_title_provider_result,
)
from elspeth.web.composer.prepared_provider_input import PreparedProviderInput, prepare_provider_input
from elspeth.web.composer.provider_audit_source import ProviderAuditSource, audit_drafts_json
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import ProviderAttempt, begin_provider_attempt_on_connection
from elspeth.web.required_work import RequiredWorkBinding
from elspeth.web.sessions._auto_title import PreparedAutoTitleRequest, prepare_auto_title_request
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.composer_operations import ComposerOperationClaim, ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    ContinuationEvent,
    StoredContinuationSnapshot,
    _effect_snapshot,
    _require_current_continuation_running,
    append_complete_snapshot,
    load_snapshot,
    private_chunk_key,
    put_chunk,
    read_chunk,
)
from elspeth.web.sessions.continuation_bootstrap_source import (
    _pretitle_checkpoint_at_cursor,
    _pretitle_recovery_origin,
    read_pretitle_bootstrap_on_connection,
    require_current_prepared_title_values_on_connection,
    require_pretitle_publication_lineage_on_connection,
)
from elspeth.web.sessions.models import (
    chat_messages_table as messages,
)
from elspeth.web.sessions.models import (
    composer_async_operations_table as jobs,
)
from elspeth.web.sessions.models import (
    composer_continuation_chunks_table as chunks,
)
from elspeth.web.sessions.models import (
    composer_continuation_effects_table as effects,
)
from elspeth.web.sessions.models import (
    composer_continuation_events_table as events,
)
from elspeth.web.sessions.models import (
    composer_continuation_provider_ordinals_table as ordinals,
)
from elspeth.web.sessions.models import (
    composer_operation_segments_table as segments,
)
from elspeth.web.sessions.models import (
    composer_segment_attempts_table as attempts,
)
from elspeth.web.sessions.models import (
    quota_provider_attempts_table as physical,
)
from elspeth.web.sessions.models import (
    sessions_table as sessions,
)
from elspeth.web.sessions.models import (
    token_usage_ledger_table as usage,
)
from elspeth.web.sessions.time_normalization import restore_utc

if TYPE_CHECKING:
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
    from elspeth.web.sessions.service import SessionServiceImpl


class TitleInputDescriptor(ContinuationValue):
    schema_version: Literal["composer.title-input.v1"] = "composer.title-input.v1"
    wire_json: str
    input_sha256: str
    guard_mode: str
    guard_fingerprint: str | None
    guard_timeout: float | None
    provider_timeout: float


class TitleTransition(ContinuationValue):
    schema_version: Literal["composer.title-transition.v1"] = "composer.title-transition.v1"
    stage: Literal["prepared", "admitted", "received", "settled"]
    predecessor_checkpoint_id: str
    predecessor_cursor: int = Field(ge=0)
    input: PrivateChunkRef
    creator_sequence: int = Field(ge=1)
    creator_generation: int = Field(ge=1)


@dataclass(frozen=True, slots=True)
class PreparedTitleProvider:
    running: ComposerOperationRunning
    selected: StoredContinuationSnapshot
    composer: ComposerServiceImpl = field(repr=False)
    authority: ComposerAsyncOperationAuthority = field(repr=False)
    names: frozenset[str]
    work: RequiredWorkBinding = field(repr=False)
    actor_user_id: str
    request: PreparedAutoTitleRequest = field(repr=False)
    input: PrivateChunk
    clock: ContinuationActionClock = field(repr=False)
    timing: ContinuationTimingObservation
    provider_input: PreparedProviderInput = field(repr=False)


@dataclass(frozen=True, slots=True)
class TitleProviderAdmission:
    attempt: ProviderAttempt
    selected: StoredContinuationSnapshot


@dataclass(frozen=True, slots=True)
class CurrentTitleNames:
    running: ComposerOperationRunning
    selected: StoredContinuationSnapshot
    names: frozenset[str]

    def require(self, conn: Connection) -> None:
        if type(self.names) is not frozenset or any(type(name) is not str for name in self.names):
            raise AuditIntegrityError("TITLE lost its actual names-only producer value")
        _current(conn, self.running, self.selected, owed=True)


@dataclass(frozen=True, slots=True)
class TitleAuditSettlementRequest:
    running: ComposerOperationRunning
    selected: StoredContinuationSnapshot
    result: PrivateChunk
    audit_call: ComposerLLMCall
    audit_drafts: tuple[AuditMessageDraft, ...]
    inventory: CurrentTitleNames | None = None


@dataclass(frozen=True, slots=True)
class CurrentTitleResult:
    selected: StoredContinuationSnapshot
    settlement: TitleAuditSettlementRequest
    settled: bool


def require_title_creator(running: ComposerOperationRunning) -> tuple[int, int]:
    if type(running) is not ComposerOperationRunning or type(running.claim) is not ComposerOperationClaim:
        raise AuditIntegrityError("TITLE creator requires its actual current running owner")
    sequence = running.claim.segment_sequence
    generation = running.claim.ownership_generation
    if type(sequence) is not int or sequence < 1 or type(generation) is not int or generation < 1:
        raise AuditIntegrityError("TITLE creator lost its actual positive sequence/generation")
    return sequence, generation


def title_input_descriptor(
    composer: ComposerServiceImpl, request: PreparedAutoTitleRequest, *, prepared_input: PreparedProviderInput
) -> PrivateChunk:
    from elspeth.web.composer.provider_gateway import _apply_openrouter_app_identity, _apply_openrouter_usage_accounting
    from elspeth.web.composer.service import ComposerServiceImpl

    if type(composer) is not ComposerServiceImpl or type(request) is not PreparedAutoTitleRequest:
        raise AuditIntegrityError("TITLE input requires the actual current producer")
    if request.kwargs["messages"] is not request.audit_messages or request.kwargs["timeout"] != 5.0:
        raise AuditIntegrityError("TITLE input changed its original request identity/timeout")
    if (
        type(prepared_input) is not PreparedProviderInput
        or prepared_input.byte_limit != composer._settings.composer_max_prepared_request_bytes
        or ("tools" in request.kwargs) != prepared_input.tools_present
    ):
        raise AuditIntegrityError("TITLE input changed its actual current preparation")
    prepared_input.verify(
        model=request.kwargs["model"],
        role="composer",
        mount=composer.composer_prompt_guard_mount,
        mode=composer._settings.composer_prompt_guard_mode,
        user_prompt=request.user_prompt,
    )
    wire = {**request.kwargs, "messages": prepared_input.evidence.messages}
    if prepared_input.tools_present:
        wire["tools"] = prepared_input.tools
    _apply_openrouter_app_identity(wire)
    _apply_openrouter_usage_accounting(wire)
    mount = composer.composer_prompt_guard_mount
    descriptor = TitleInputDescriptor(
        wire_json=continuation_provider_input_json(wire),
        input_sha256=continuation_provider_input_hash(wire),
        guard_mode=composer._settings.composer_prompt_guard_mode,
        guard_fingerprint=mount.client.policy_fingerprint if mount is not None else None,
        guard_timeout=mount.client.timeout_seconds if mount is not None else None,
        provider_timeout=5.0,
    )
    return encode_private_chunk(codec="composer.title-input.v1", canonical=canonical_json(descriptor.model_dump(mode="json")))


def _descriptor(chunk: PrivateChunk) -> TitleInputDescriptor:
    if type(chunk) is not PrivateChunk or chunk.reference.codec != "composer.title-input.v1":
        raise AuditIntegrityError("TITLE source lost its exact input codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    try:
        result = TitleInputDescriptor.model_validate_json(chunk.canonical, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("TITLE source input violates its complete owned shape") from error
    value, parse_error = parse_json_strict(result.wire_json)
    from elspeth.contracts.hashing import stable_hash

    if (
        parse_error is not None
        or type(value) is not dict
        or stable_hash(value) != result.input_sha256
        or canonical_json(result.model_dump(mode="json")) != chunk.canonical
        or result.provider_timeout != 5.0
    ):
        raise AuditIntegrityError("TITLE source input changed its exact canonical identity")
    return result


def _chunk(conn: Connection, running: ComposerOperationRunning, ref: PrivateChunkRef) -> PrivateChunk:
    if type(ref) is not PrivateChunkRef:
        raise AuditIntegrityError("TITLE private source requires its exact nominal reference")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    key = f"value:{ref.codec}:{ref.digest}"
    codec = conn.execute(
        select(chunks.c.codec).where(chunks.c.session_id == sid, chunks.c.operation_id == operation, chunks.c.chunk_key == key)
    ).scalar_one_or_none()
    if codec != ref.codec:
        raise AuditIntegrityError("TITLE source changed its actual SQL chunk codec")
    try:
        text = read_chunk(conn, session_id=sid, operation_id=operation, key=key).decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise AuditIntegrityError("TITLE source is not strict UTF-8") from error
    verify_private_chunk(ref, text)
    return PrivateChunk(ref, text)


def _frame(selected: StoredContinuationSnapshot) -> InitializeFrame:
    if type(selected) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("TITLE source requires its complete nominal selected root")
    root = decode_continuation_snapshot(selected.payload_json)
    frame = root.frames[0]
    if (
        len(root.frames) != 1
        or type(frame) is not InitializeFrame
        or frame.ingress_phase != "title_pending"
        or frame.bootstrap is None
        or frame.ingress_receipt_json is None
        or root.pending_owner_results
        or root.semantic_frontier is not None
        or root.active_action_json is not None
        or root.active_action_source is not None
    ):
        raise AuditIntegrityError("TITLE owner requires its sole complete title-pending Initialize frame")
    return frame


def _current(conn: Connection, running: ComposerOperationRunning, selected: StoredContinuationSnapshot, *, owed: bool = False) -> None:
    if type(running) is not ComposerOperationRunning or type(selected) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("TITLE current read requires actual nominal current/selected owners")
    _require_current_continuation_running(conn, running, allow_cancel=owed, allow_expired_segment=owed)
    if load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id) != selected:
        raise ContinuationCASConflict("TITLE source changed its complete current root CAS")
    _frame(selected)


def _event(transition: TitleTransition) -> str:
    return canonical_json({"owner": "composer_title_provider", "transition": transition.model_dump(mode="json")})


def _append(
    conn: Connection,
    running: ComposerOperationRunning,
    selected: StoredContinuationSnapshot,
    following: str,
    descriptor: PrivateChunkRef,
    stage: Literal["prepared", "admitted", "received", "settled"],
) -> StoredContinuationSnapshot:
    sequence, generation = require_title_creator(running)
    return append_complete_snapshot(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.cursor,
        snapshot_json=following,
        event_batch=(
            ContinuationEvent(
                "checkpoint",
                _event(
                    TitleTransition(
                        stage=stage,
                        predecessor_checkpoint_id=selected.checkpoint_id,
                        predecessor_cursor=selected.cursor,
                        input=descriptor,
                        creator_sequence=sequence,
                        creator_generation=generation,
                    )
                ),
            ),
        ),
        handoff=False,
    )


def _timing(conn: Connection, running: ComposerOperationRunning, selected: StoredContinuationSnapshot) -> ContinuationTimingObservation:
    root = decode_continuation_snapshot(selected.payload_json)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    row = conn.execute(
        select(segments).where(
            segments.c.session_id == str(running.claim.session_id),
            segments.c.operation_id == running.claim.operation_id,
            segments.c.sequence == running.claim.segment_sequence,
            segments.c.generation == running.claim.ownership_generation,
        )
    ).one()
    if row.deadline_at is None or row.cooperative_at is None or row.policy_sha256 != root.policy_hash:
        raise AuditIntegrityError("TITLE lost its original actual segment clock/policy")
    return freeze_observation(
        sequence=row.sequence,
        generation=row.generation,
        now=database_now(conn),
        deadline=restore_utc(row.deadline_at),
        cooperative=restore_utc(row.cooperative_at),
        watermark=policy.segment.watermark,
    )


def prepare_title_provider_on_connection(
    conn: Connection,
    composer: ComposerServiceImpl,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    clock: ContinuationActionClock,
    authority: ComposerAsyncOperationAuthority,
    inventory: CurrentContinuationEnvInventory,
    work: RequiredWorkBinding,
) -> PreparedTitleProvider:
    from elspeth.web.sessions.schemas import SendMessageRequest
    from elspeth.web.sessions.service import SessionServiceImpl

    service = composer._require_sessions_service()
    if type(service) is not SessionServiceImpl:
        raise AuditIntegrityError("pre-title bootstrap requires the actual sessions SQL owner")
    stored = read_pretitle_bootstrap_on_connection(conn, service, running, selected=selected)
    root = decode_continuation_snapshot(selected.payload_json)
    frame = _frame(selected)
    names = require_current_env_inventory_on_connection(
        conn, running, inventory=inventory, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.cursor
    )
    snapshot = decode_continuation_bootstrap(stored.bootstrap)
    values = bootstrap_values(snapshot)
    original_request = values.request
    if type(original_request) is not SendMessageRequest:
        raise AuditIntegrityError("pre-title bootstrap has no implemented Recompose source owner")
    if not snapshot.auto_title_eligible or not original_request.content.strip():
        raise AuditIntegrityError("TITLE preparation has no original eligible nonempty request")
    settings = composer._settings
    request = prepare_auto_title_request(
        user_message=original_request.content,
        model=settings.composer_model,
        temperature=settings.composer_temperature,
        seed=settings.composer_seed,
        api_base=settings.composer_endpoint_base_url,
        api_key=settings.composer_endpoint_api_key.get_secret_value() if settings.composer_endpoint_api_key is not None else None,
    )
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    if clock.policy != policy.actions.title or policy.actions.title.guard is None or policy.actions.title.quota is None:
        raise AuditIntegrityError("TITLE preparation changed its retained action allowance")
    from elspeth.web.composer.continuation_clock import SegmentFit, admit_action

    timing = _timing(conn, running, selected)
    if (
        admit_action(
            policy.actions.title,
            policy.segment,
            hard_remaining=timing.hard_remaining_seconds,
            cooperative_remaining=timing.cooperative_remaining_seconds,
            fresh=False,
        )
        is not SegmentFit.ADMIT
    ):
        raise AuditIntegrityError("TITLE action must yield before new provider preparation")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    if (
        conn.execute(
            select(ordinals).where(ordinals.c.session_id == sid, ordinals.c.operation_id == operation, ordinals.c.family == 3)
        ).first()
        is not None
    ):
        raise AuditIntegrityError("TITLE may not reset or reuse its durable family ordinal")
    # Current source, action policy/timing and ordinal refusal precede a new
    # ordinary size refusal. No descriptor/request copy or durable preparation
    # write is needed to establish those existing admissions.
    provider_input = prepare_provider_input(
        messages=request.audit_messages,
        tools=request.kwargs["tools"] if "tools" in request.kwargs else None,
        tools_present="tools" in request.kwargs,
        model=request.kwargs["model"],
        role="composer",
        mount=composer.composer_prompt_guard_mount,
        mode=settings.composer_prompt_guard_mode,
        user_prompt=request.user_prompt,
        max_bytes=settings.composer_max_prepared_request_bytes,
    )
    descriptor = title_input_descriptor(composer, request, prepared_input=provider_input)
    value = _descriptor(descriptor)
    conn.execute(insert(ordinals).values(session_id=sid, operation_id=operation, family=3, next_ordinal=1))
    conn.execute(
        insert(effects).values(
            session_id=sid,
            operation_id=operation,
            effect_key="provider:3:0",
            owner="provider",
            disposition="prepared",
            input_sha256=value.input_sha256,
            creator_sequence=running.claim.segment_sequence,
            creator_generation=running.claim.ownership_generation,
        )
    )
    put_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=private_chunk_key(descriptor),
        payload=descriptor.canonical.encode("utf-8"),
        codec=descriptor.reference.codec,
    )
    following = root.model_copy(
        update={"frames": (frame.model_copy(update={"provider_effect_key": "provider:3:0", "provider_input_sha256": value.input_sha256}),)}
    )
    encoded = _effect_snapshot(encode_continuation_snapshot(following), effect_key="provider:3:0", disposition="prepared", receipt_id=None)
    following_selected = _append(conn, running, selected, encoded, descriptor.reference, "prepared")
    return PreparedTitleProvider(
        running,
        following_selected,
        composer,
        authority,
        names,
        work,
        snapshot.actor_user_id,
        request,
        descriptor,
        clock,
        timing,
        provider_input,
    )


def _source_chain(
    conn: Connection,
    running: ComposerOperationRunning,
    selected: StoredContinuationSnapshot,
    *,
    names: frozenset[str],
    generation: int | None = None,
) -> tuple[TitleInputDescriptor, PrivateChunkRef, str, StoredContinuationSnapshot]:
    """Verify every exact stage and genuine recovery edge back to publication."""
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == "provider:3:0")
    ).one_or_none()
    if (
        effect is None
        or effect.owner != "provider"
        or effect.action_json is not None
        or effect.committed_state_ids_json is not None
        or effect.graph_sha256 is not None
    ):
        raise AuditIntegrityError("TITLE lost its original provider effect owner")
    all_effects = conn.execute(select(effects.c.effect_key).where(effects.c.session_id == sid, effects.c.operation_id == operation)).all()
    family_rows = conn.execute(
        select(ordinals.c.family, ordinals.c.next_ordinal).where(ordinals.c.session_id == sid, ordinals.c.operation_id == operation)
    ).all()
    source_fences = select(attempts.c.session_operation_id).where(attempts.c.session_id == sid, attempts.c.operation_id == operation)
    physical_ids = tuple(
        conn.execute(
            select(physical.c.attempt_id).where(physical.c.session_id == sid, physical.c.operation_id.in_(source_fences))
        ).scalars()
    )
    if (
        tuple(row.effect_key for row in all_effects) != ("provider:3:0",)
        or tuple((row.family, row.next_ordinal) for row in family_rows) != ((3, 1),)
        or physical_ids != (() if effect.provider_attempt_id is None else (effect.provider_attempt_id,))
    ):
        raise AuditIntegrityError("TITLE source has another effect/ordinal/physical owner outside its complete closed cohort")
    current = selected
    if generation is None:
        generation = running.claim.ownership_generation
    expected_stage: str | None = None
    descriptor_ref: PrivateChunkRef | None = None
    final_stage: str | None = None
    while True:
        origin, generation = _pretitle_recovery_origin(conn, running, current, generation=generation)
        row = conn.execute(
            select(events).where(events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == origin.cursor)
        ).one_or_none()
        if row is None or row.tag != "checkpoint":
            raise AuditIntegrityError("TITLE source lost its actual stage event")
        try:
            event_text = read_chunk(conn, session_id=sid, operation_id=operation, key=row.payload_chunk_key).decode(
                "utf-8", errors="strict"
            )
        except UnicodeDecodeError as error:
            raise AuditIntegrityError("TITLE stage event is not strict UTF-8") from error
        value, parse_error = parse_json_strict(event_text)
        if parse_error is not None or type(value) is not dict or set(value) != {"owner", "transition"}:
            raise AuditIntegrityError("TITLE source lost its complete stage event")
        try:
            transition = TitleTransition.model_validate_json(canonical_json(value["transition"]), strict=True)
        except ValidationError as error:
            raise AuditIntegrityError("TITLE stage event violates its owned shape") from error
        if (
            event_text != _event(transition)
            or transition.creator_sequence != running.claim.segment_sequence
            or transition.creator_generation != generation
            or transition.predecessor_cursor + 1 != origin.cursor
            or (expected_stage is not None and transition.stage != expected_stage)
            or (descriptor_ref is not None and transition.input != descriptor_ref)
        ):
            raise AuditIntegrityError("TITLE source changed its complete ordered stage/creator lineage")
        descriptor_ref = transition.input
        if final_stage is None:
            final_stage = transition.stage
        prior = _pretitle_checkpoint_at_cursor(conn, session_id=sid, operation_id=operation, cursor=transition.predecessor_cursor)
        if prior.checkpoint_id != transition.predecessor_checkpoint_id:
            raise AuditIntegrityError("TITLE source changed its exact original predecessor")
        before = decode_continuation_snapshot(prior.payload_json)
        decode_continuation_snapshot(origin.payload_json)
        before_frame = before.frames[0]
        after_frame = _frame(origin)
        descriptor = _descriptor(_chunk(conn, running, descriptor_ref))
        if type(before_frame) is not InitializeFrame or before_frame.bootstrap != after_frame.bootstrap:
            raise AuditIntegrityError("TITLE stage changed its complete bootstrap owner")
        if transition.stage == "prepared":
            if (
                before_frame.provider_effect_key is not None
                or before_frame.title_attempt_id is not None
                or before_frame.title_response is not None
                or before.effect_frontier
                or before.pending_settlement_ids
                or (effect.creator_sequence, effect.creator_generation) != (transition.creator_sequence, transition.creator_generation)
            ):
                raise AuditIntegrityError("TITLE source is not its real provider-free preparation predecessor")
            following = before.model_copy(
                update={
                    "frames": (
                        before_frame.model_copy(
                            update={"provider_effect_key": "provider:3:0", "provider_input_sha256": descriptor.input_sha256}
                        ),
                    )
                }
            )
            expected = _effect_snapshot(
                encode_continuation_snapshot(following), effect_key="provider:3:0", disposition="prepared", receipt_id=None
            )
        elif transition.stage == "admitted":
            if effect.provider_attempt_id is None or before_frame.title_attempt_id is not None or before_frame.title_response is not None:
                raise AuditIntegrityError("TITLE admitted source lost its original physical association")
            following = before.model_copy(
                update={"frames": (before_frame.model_copy(update={"title_attempt_id": effect.provider_attempt_id}),)}
            )
            expected = _effect_snapshot(
                encode_continuation_snapshot(following),
                effect_key="provider:3:0",
                disposition="dispatched",
                receipt_id=None,
                add_attempt_id=effect.provider_attempt_id,
            )
            expected_stage = "prepared"
        elif transition.stage == "received":
            if before_frame.title_response is not None or after_frame.title_response is None or effect.receipt_key is None:
                raise AuditIntegrityError("TITLE received source lost its actual full response")
            following = before.model_copy(
                update={"frames": (before_frame.model_copy(update={"title_response": after_frame.title_response}),)}
            )
            expected = _effect_snapshot(
                encode_continuation_snapshot(following), effect_key="provider:3:0", disposition="committed", receipt_id=effect.receipt_key
            )
            expected_stage = "admitted"
        else:
            if before_frame.title_response is None or effect.provider_attempt_id not in before.pending_settlement_ids:
                raise AuditIntegrityError("TITLE settled source lost its original pending accounting owner")
            expected = _effect_snapshot(
                prior.payload_json,
                effect_key="provider:3:0",
                disposition="committed",
                receipt_id=effect.receipt_key,
                remove_attempt_id=effect.provider_attempt_id,
            )
            expected_stage = "received"
        expected_root = decode_continuation_snapshot(expected).model_copy(update={"cursor": origin.cursor})
        if encode_continuation_snapshot(expected_root) != origin.payload_json or effect.input_sha256 != descriptor.input_sha256:
            raise AuditIntegrityError("TITLE stage changed its full original root/receipt/input transition")
        if transition.stage == "prepared":
            if before_frame.bootstrap is None:
                raise AuditIntegrityError("TITLE source lost its actual original publication")
            bootstrap = _chunk(conn, running, before_frame.bootstrap)
            require_pretitle_publication_lineage_on_connection(conn, running, selected=prior, chunk=bootstrap, generation=generation)
            snapshot = decode_continuation_bootstrap(bootstrap)
            parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
            principal = conn.execute(select(sessions.c.user_id, sessions.c.archived_at).where(sessions.c.id == sid)).one()
            if (
                snapshot.logical != before.logical
                or snapshot.actor_user_id != parent.actor_user_id
                or principal.user_id != parent.actor_user_id
                or principal.archived_at is not None
                or before_frame.request_json != parent.request_json
                or not snapshot.auto_title_eligible
            ):
                raise AuditIntegrityError("TITLE source changed its original logical request/actor/eligibility")
            # Credential admission consumes actual current names, never an empty fallback.
            from elspeth.web.credential_guard import require_no_credential_material

            require_no_credential_material(
                parse_json_strict(descriptor.wire_json)[0], surface="continuation_title_input", env_ref_names=names
            )
            return descriptor, descriptor_ref, final_stage, prior
        current = prior


def admit_title_provider_on_connection(
    conn: Connection, service: SessionServiceImpl, prepared: PreparedTitleProvider, *, model: str
) -> TitleProviderAdmission:
    if type(prepared) is not PreparedTitleProvider or prepared.composer._require_sessions_service() is not service:
        raise AuditIntegrityError("TITLE physical admission changed its actual prepared owner")
    running, selected = prepared.running, prepared.selected
    _current(conn, running, selected)
    frame = _frame(selected)
    if frame.bootstrap is None:
        raise AuditIntegrityError("TITLE physical admission lost its actual bootstrap")
    if title_input_descriptor(prepared.composer, prepared.request, prepared_input=prepared.provider_input) != prepared.input:
        raise AuditIntegrityError("TITLE original request/guard policy changed before physical admission")
    _, ref, stage, source = _source_chain(conn, running, selected, names=prepared.names)
    if stage != "prepared" or ref != prepared.input.reference or prepared.request.kwargs["model"] != model:
        raise AuditIntegrityError("TITLE may dispatch only its same-claim original prepared request")
    require_current_prepared_title_values_on_connection(
        conn, service, running, selected=selected, source=source, chunk=_chunk(conn, running, frame.bootstrap)
    )
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == "provider:3:0")
    ).one()
    if (
        effect.disposition != "prepared"
        or effect.creator_sequence != running.claim.segment_sequence
        or effect.creator_generation != running.claim.ownership_generation
        or effect.provider_attempt_id is not None
        or effect.receipt_key is not None
        or effect.result_chunk_key is not None
        or effect.physical_sequence is not None
        or effect.physical_generation is not None
        or effect.requested_model is not None
        or effect.response_finished_at is not None
    ):
        raise AuditIntegrityError("TITLE refuses foreign or already dispatched provider preparation")
    _timing(conn, running, selected)
    attempt = begin_provider_attempt_on_connection(
        conn,
        session_operation_context=running.session_operation_context,
        source="auto_title",
        policy=service._chargeable_admission_policy,
        run_id=None,
    )
    conn.execute(
        update(effects)
        .where(
            effects.c.session_id == sid,
            effects.c.operation_id == operation,
            effects.c.effect_key == "provider:3:0",
            effects.c.disposition == "prepared",
        )
        .values(
            disposition="dispatched",
            provider_attempt_id=attempt.attempt_id,
            physical_sequence=running.claim.segment_sequence,
            physical_generation=running.claim.ownership_generation,
            requested_model=model,
        )
    )
    root = decode_continuation_snapshot(selected.payload_json)
    frame = _frame(selected)
    following = root.model_copy(update={"frames": (frame.model_copy(update={"title_attempt_id": attempt.attempt_id}),)})
    encoded = _effect_snapshot(
        encode_continuation_snapshot(following),
        effect_key="provider:3:0",
        disposition="dispatched",
        receipt_id=None,
        add_attempt_id=attempt.attempt_id,
    )
    return TitleProviderAdmission(attempt, _append(conn, running, selected, encoded, ref, "admitted"))


def retain_title_response_on_connection(
    conn: Connection,
    prepared: PreparedTitleProvider,
    *,
    selected: StoredContinuationSnapshot,
    captured: CapturedTitleProviderResult,
    names: frozenset[str],
) -> TitleAuditSettlementRequest:
    running = prepared.running
    _current(conn, running, selected, owed=True)
    descriptor, ref, stage, _source = _source_chain(conn, running, selected, names=names)
    if stage != "admitted" or ref != prepared.input.reference or type(captured) is not CapturedTitleProviderResult:
        raise AuditIntegrityError("TITLE response retention lacks its original admitted producer")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == "provider:3:0")
    ).one()
    result = decode_title_provider_result(captured.chunk, env_ref_names=names)
    if result.completion.tool_batch.calls:
        raise AuditIntegrityError("TITLE response cannot introduce an unrequested tool batch")
    _require_physical(conn, running, effect, result.audit_call, result.source, descriptor)
    snapshot = result.snapshot
    if (
        effect.disposition != "dispatched"
        or effect.receipt_key is not None
        or effect.result_chunk_key is not None
        or effect.response_finished_at is not None
        or snapshot.logical != decode_continuation_snapshot(selected.payload_json).logical
        or snapshot.effect_key != effect.effect_key
        or snapshot.input_sha256 != descriptor.input_sha256
        or (snapshot.creator_sequence, snapshot.creator_generation) != (effect.creator_sequence, effect.creator_generation)
        or result.audit_call != captured.audit_call
        or audit_drafts_json(result.audit_drafts) != audit_drafts_json(captured.audit_drafts)
    ):
        raise AuditIntegrityError("TITLE response changed its actual complete original physical producer")
    parent_actor = conn.execute(select(jobs.c.actor_user_id).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).scalar_one()
    if snapshot.actor_user_id != parent_actor:
        raise AuditIntegrityError("TITLE response changed its original actor")
    put_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=private_chunk_key(captured.chunk),
        payload=captured.chunk.canonical.encode("utf-8"),
        codec=captured.chunk.reference.codec,
    )
    receipt = f"title:{captured.chunk.reference.digest}"
    conn.execute(
        update(effects)
        .where(
            effects.c.session_id == sid,
            effects.c.operation_id == operation,
            effects.c.effect_key == effect.effect_key,
            effects.c.disposition == "dispatched",
        )
        .values(
            disposition="committed",
            receipt_key=receipt,
            result_chunk_key=private_chunk_key(captured.chunk),
            response_finished_at=captured.audit_call.finished_at,
        )
    )
    root = decode_continuation_snapshot(selected.payload_json)
    following = root.model_copy(update={"frames": (_frame(selected).model_copy(update={"title_response": captured.chunk.reference}),)})
    encoded = _effect_snapshot(
        encode_continuation_snapshot(following), effect_key=effect.effect_key, disposition="committed", receipt_id=receipt
    )
    following_selected = _append(conn, running, selected, encoded, ref, "received")
    return TitleAuditSettlementRequest(running, following_selected, captured.chunk, captured.audit_call, captured.audit_drafts)


def _require_physical(
    conn: Connection,
    running: ComposerOperationRunning,
    effect: Row[Any],
    call: ComposerLLMCall,
    source: ProviderAuditSource,
    descriptor: TitleInputDescriptor,
) -> None:
    # The effect/physical rows are actual SQL Rows returned only by this owner.
    from elspeth.contracts.hashing import stable_hash
    from elspeth.web.composer.provider_context import capture_provider_context

    wire, error = parse_json_strict(descriptor.wire_json)
    if (
        error is not None
        or type(wire) is not dict
        or set(wire) != {"schema", "messages", "tools_present", "tools", "options"}
        or wire["schema"] != "composer.provider-input.v1"
        or type(wire["messages"]) is not list
        or wire["tools_present"] is not False
        or wire["tools"] is not None
        or type(wire["options"]) is not dict
    ):
        raise AuditIntegrityError("TITLE source lost its original complete tool-less input")
    options = wire["options"]
    if (
        call.model_requested != options["model"]
        or call.temperature != options.get("temperature")
        or call.seed != options.get("seed")
        or call.messages_hash != stable_hash(wire["messages"])
        or call.tools_spec_hash is not None
        or call.declared_tool_names
        or call.private_request != capture_provider_context(messages=wire["messages"], tools=None)
        or (effect.physical_sequence, effect.physical_generation) != (effect.creator_sequence, effect.creator_generation)
    ):
        raise AuditIntegrityError("TITLE original audit differs from its complete original request/context/sampling")
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == call.call_id)).one_or_none()
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == str(running.claim.session_id),
            attempts.c.operation_id == running.claim.operation_id,
            attempts.c.sequence == effect.physical_sequence,
            attempts.c.generation == effect.physical_generation,
        )
    ).one_or_none()
    actor = conn.execute(
        select(jobs.c.actor_user_id).where(
            jobs.c.session_id == str(running.claim.session_id), jobs.c.operation_id == running.claim.operation_id
        )
    ).scalar_one()
    if (
        physical_row is None
        or creator is None
        or physical_row.source != "auto_title"
        or physical_row.run_id is not None
        or physical_row.identity_id != actor
        or physical_row.session_id != str(running.claim.session_id)
        or physical_row.attempt_id != effect.provider_attempt_id
        or (source.session_id, source.operation_id, source.operation_epoch)
        != (creator.session_id, creator.session_operation_id, creator.session_operation_epoch)
        or physical_row.operation_id != creator.session_operation_id
        or physical_row.operation_epoch != creator.session_operation_epoch
        or physical_row.lease_token != creator.session_operation_lease_token
        or restore_utc(physical_row.started_at) != call.started_at
        or effect.requested_model != call.model_requested
    ):
        raise AuditIntegrityError("TITLE result lost its original physical audit/creator/source association")


def _read_title_result_source_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot, inventory: CurrentTitleNames
) -> CurrentTitleResult:
    if type(inventory) is not CurrentTitleNames or inventory.running is not running or inventory.selected != selected:
        raise AuditIntegrityError("TITLE result lost its actual current names/source bracket")
    inventory.require(conn)
    names = inventory.names
    _current(conn, running, selected, owed=True)
    chunk, result, settled = _read_title_result_data_on_connection(conn, running, selected=selected, names=names)
    return CurrentTitleResult(
        selected, TitleAuditSettlementRequest(running, selected, chunk, result.audit_call, result.audit_drafts, inventory), settled
    )


def _read_title_result_data_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    names: frozenset[str],
    generation: int | None = None,
) -> tuple[PrivateChunk, DecodedTitleProviderResult, bool]:
    """Historical DATA checks only; the caller retains actual current-root authority.

    The default is the original current TITLE source. A historical generation
    is usable only after its caller proves the complete actual forward/current
    lineage; it never materializes an old context or creates a current grant.
    """
    descriptor, _ref, stage, _source = _source_chain(conn, running, selected, names=names, generation=generation)
    if stage not in ("received", "settled"):
        raise AuditIntegrityError("TITLE response is unresolved; wait without another SDK dispatch")
    frame = _frame(selected)
    if frame.title_response is None:
        raise AuditIntegrityError("TITLE committed result lost its selected response")
    chunk = _chunk(conn, running, frame.title_response)
    result = decode_title_provider_result(chunk, env_ref_names=names)
    if result.completion.tool_batch.calls:
        raise AuditIntegrityError("TITLE retained result cannot introduce an unrequested tool batch")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == "provider:3:0")
    ).one()
    _require_physical(conn, running, effect, result.audit_call, result.source, descriptor)
    parent_actor = conn.execute(select(jobs.c.actor_user_id).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).scalar_one()
    snapshot = result.snapshot
    if (
        effect.disposition != "committed"
        or effect.receipt_key != f"title:{chunk.reference.digest}"
        or effect.result_chunk_key != private_chunk_key(chunk)
        or effect.input_sha256 != descriptor.input_sha256
        or snapshot.logical != decode_continuation_snapshot(selected.payload_json).logical
        or snapshot.actor_user_id != parent_actor
        or snapshot.effect_key != effect.effect_key
        or snapshot.input_sha256 != descriptor.input_sha256
        or (snapshot.creator_sequence, snapshot.creator_generation) != (effect.creator_sequence, effect.creator_generation)
        or effect.response_finished_at is None
        or restore_utc(effect.response_finished_at) != result.audit_call.finished_at
    ):
        raise AuditIntegrityError("TITLE current result changed its complete response/receipt/source/actor")
    settled = stage == "settled"
    return chunk, result, settled


def read_title_result_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot, inventory: CurrentTitleNames
) -> CurrentTitleResult:
    current = _read_title_result_source_on_connection(conn, running, selected=selected, inventory=inventory)
    _require_audit_cohort(conn, running, current.settlement.audit_call, current.settlement.audit_drafts, settled=current.settled)
    return current


def _require_audit_cohort(
    conn: Connection, running: ComposerOperationRunning, call: ComposerLLMCall, drafts: tuple[AuditMessageDraft, ...], *, settled: bool
) -> None:
    from collections.abc import Mapping

    from elspeth.contracts.freeze import deep_thaw
    from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence, is_diagnostic_evidence_envelope
    from elspeth.web.composer.visible_tool_event import VISIBLE_RESPONSE_GAP_KIND, validate_visible_response_gap
    from elspeth.web.coordination.quota_authority import token_usage_entry_from_llm_call_envelope

    if call.call_id is None:
        raise AuditIntegrityError("TITLE accounting lost its actual physical call identity")
    expected = parse_json_strict(audit_drafts_json(drafts))[0]
    evidence_ids: set[str] = set()
    for draft in drafts:
        for envelope in draft.tool_calls or ():
            if is_diagnostic_evidence_envelope(envelope):
                evidence_ids.add(
                    ComposerDiagnosticEvidence.from_envelope(envelope, expected_session_id=str(running.claim.session_id)).evidence_id
                )
    actual: list[object] = []
    rows = conn.execute(
        select(messages).where(messages.c.session_id == str(running.claim.session_id)).order_by(messages.c.sequence_no)
    ).all()
    for row in rows:
        # Classify this heterogeneous owned union before interpreting required
        # audit fields. An invalid known kind is corruption, not unrelated DATA.
        public_envelopes = []
        for envelope in row.tool_calls or ():
            if envelope.get("_kind") == "llm_call_audit":
                try:
                    public_call = envelope["call"]
                    if not isinstance(public_call, Mapping):
                        raise TypeError("Stored public LLM call must have its owned call mapping")
                    if "call_id" not in public_call:
                        raise KeyError("call_id")
                    # The existing parser owns required accounting fields and
                    # explicit legacy null IDs. Do not invent a full new schema.
                    token_usage_entry_from_llm_call_envelope(envelope)
                except (KeyError, TypeError, ValueError) as error:
                    raise AuditIntegrityError("TITLE stored public LLM-call audit violates its required accounting shape") from error
                public_envelopes.append(envelope)
        for envelope in row.tool_calls or ():
            if envelope.get("_kind") == VISIBLE_RESPONSE_GAP_KIND:
                if len(public_envelopes) != 1:
                    raise AuditIntegrityError("TITLE stored response gap lacks its one actual public-call companion")
                validate_visible_response_gap(envelope, public_envelopes[0])
        related = False
        for envelope in row.tool_calls or ():
            public_call = envelope.get("call")
            if envelope.get("provider_call_id") == call.call_id or (
                isinstance(public_call, Mapping) and public_call.get("call_id") == call.call_id
            ):
                related = True
            if is_diagnostic_evidence_envelope(envelope):
                evidence = ComposerDiagnosticEvidence.from_envelope(envelope, expected_session_id=str(running.claim.session_id))
                if evidence.evidence_id in evidence_ids:
                    related = True
        if related:
            if row.writer_principal != "compose_loop" or row.raw_content is not None or row.composition_state_id is not None:
                raise AuditIntegrityError("TITLE retained audit row changed its original physical custody/state")
            # Actual SQL has no inherit flag; it has already applied the None physical state binding.
            actual.append(
                {
                    "role": row.role,
                    "content": row.content,
                    "tool_calls": deep_thaw(row.tool_calls),
                    "tool_call_id": row.tool_call_id,
                    "parent_assistant_id": row.parent_assistant_id,
                    "composition_state_id": row.composition_state_id,
                    "inherit_composition_state": drafts[len(actual)].inherit_composition_state if len(actual) < len(drafts) else True,
                }
            )
    attempt = conn.execute(select(physical).where(physical.c.attempt_id == call.call_id)).one()
    from elspeth.web.composer.audit import llm_call_audit_envelope
    from elspeth.web.coordination.quota_authority import _usage_entry_id

    entry_id = _usage_entry_id(session_id=str(running.claim.session_id), source="auto_title", run_id=None, call_id=call.call_id)
    ledger = conn.execute(select(usage).where(usage.c.entry_id == entry_id)).all()
    if not settled:
        if actual or attempt.settled_at is not None or attempt.ledger_entry_id is not None or ledger:
            raise AuditIntegrityError("TITLE pending result has partial or foreign settled accounting")
    else:
        entry = token_usage_entry_from_llm_call_envelope(llm_call_audit_envelope(call))
        if actual != expected or attempt.settled_at is None or len(ledger) != 1 or attempt.ledger_entry_id != entry_id:
            raise AuditIntegrityError("TITLE completed accounting lost its exact complete cohort/ledger")
        if entry is None:
            raise AuditIntegrityError("TITLE complete call has no actual usage entry")
        row = ledger[0]
        if (
            row.identity_id != attempt.identity_id
            or row.session_id != attempt.session_id
            or row.source != "auto_title"
            or row.run_id is not None
            or row.model != entry.model
            or row.prompt_tokens != entry.prompt_tokens
            or row.completion_tokens != entry.completion_tokens
            or row.cached_prompt_tokens != entry.cached_prompt_tokens
            or row.reasoning_tokens != entry.reasoning_tokens
            or restore_utc(row.recorded_at) != entry.recorded_at
        ):
            raise AuditIntegrityError("TITLE completed ledger changed its original complete physical accounting")


@dataclass(slots=True)
class AdmittedTitleAuditSource:
    connection: Connection
    transaction: object
    context: SessionOperationContext
    request: TitleAuditSettlementRequest
    source: ProviderAuditSource
    descriptor: PrivateChunkRef
    already_settled: bool
    active: bool = True

    def require(self, conn: Connection, context: SessionOperationContext, drafts: tuple[AuditMessageDraft, ...]) -> None:
        if (
            type(self.source) is not ProviderAuditSource
            or type(self.request) is not TitleAuditSettlementRequest
            or type(self.request.audit_call) is not ComposerLLMCall
            or type(self.request.result) is not PrivateChunk
            or type(self.descriptor) is not PrivateChunkRef
            or not self.active
            or conn is not self.connection
            or conn.get_transaction() is not self.transaction
            or context is not self.context
            or audit_drafts_json(drafts) != audit_drafts_json(self.request.audit_drafts)
        ):
            raise AuditIntegrityError("TITLE source admission escaped its actual current SQL transaction/cohort")
        _current(conn, self.request.running, self.request.selected, owed=True)
        if type(self.request.inventory) is not CurrentTitleNames:
            raise AuditIntegrityError("TITLE source capability lost its actual current names bracket")
        actual = _read_title_result_source_on_connection(
            conn, self.request.running, selected=self.request.selected, inventory=self.request.inventory
        )
        decoded = decode_title_provider_result(actual.settlement.result, env_ref_names=self.request.inventory.names)
        _value, descriptor, _stage, _source = _source_chain(
            conn, self.request.running, self.request.selected, names=self.request.inventory.names
        )
        if descriptor != self.descriptor:
            raise AuditIntegrityError("TITLE capability changed its original input descriptor")
        if (
            actual.settlement.result != self.request.result
            or actual.settlement.audit_call != self.request.audit_call
            or decoded.source != self.source
            or actual.settled != self.already_settled
            or audit_drafts_json(actual.settlement.audit_drafts) != audit_drafts_json(drafts)
        ):
            raise AuditIntegrityError("TITLE source capability differs from its actual SQL-issued complete result")


def admit_title_audit_source_on_connection(
    conn: Connection, context: SessionOperationContext, request: TitleAuditSettlementRequest, *, names: frozenset[str]
) -> AdmittedTitleAuditSource:
    if (
        type(request) is not TitleAuditSettlementRequest
        or type(request.running) is not ComposerOperationRunning
        or type(request.audit_call) is not ComposerLLMCall
        or type(request.result) is not PrivateChunk
        or request.running.session_operation_context is not context
    ):
        raise AuditIntegrityError("TITLE audit writer requires its actual current context")
    if type(request.inventory) is not CurrentTitleNames:
        raise AuditIntegrityError("TITLE accounting requires its actual current names producer")
    current = read_title_result_on_connection(conn, request.running, selected=request.selected, inventory=request.inventory)
    result = decode_title_provider_result(request.result, env_ref_names=names)
    if (
        current.settlement.result != request.result
        or current.settlement.audit_call != request.audit_call
        or audit_drafts_json(current.settlement.audit_drafts) != audit_drafts_json(request.audit_drafts)
    ):
        raise AuditIntegrityError("TITLE audit writer changed its immutable original cohort")
    _descriptor_value, descriptor, _stage, _source = _source_chain(conn, request.running, request.selected, names=names)
    return AdmittedTitleAuditSource(conn, conn.get_transaction(), context, request, result.source, descriptor, current.settled)


def complete_title_accounting_on_connection(conn: Connection, admitted: AdmittedTitleAuditSource) -> None:
    admitted.require(conn, admitted.context, admitted.request.audit_drafts)
    running, selected = admitted.request.running, admitted.request.selected
    _require_audit_cohort(conn, running, admitted.request.audit_call, admitted.request.audit_drafts, settled=True)
    if admitted.already_settled:
        return
    encoded = _effect_snapshot(
        selected.payload_json,
        effect_key="provider:3:0",
        disposition="committed",
        receipt_id=f"title:{admitted.request.result.reference.digest}",
        remove_attempt_id=admitted.request.audit_call.call_id,
    )
    _append(conn, running, selected, encoded, admitted.descriptor, "settled")
