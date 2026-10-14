"""Request-owned saved-effect evidence; provider proposals are never receipts."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, final

from pydantic import JsonValue

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.composer.discovery_response import AdmittedDiscoveryResult, serialize_admitted_discovery_result
from elspeth.web.composer.protocol import RetainedOperationContext
from elspeth.web.composer.redaction import GetBlobContentDataModel
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._registry import response_contract_for
from elspeth.web.sessions._persist_payload import CommittedCheckpointWitness
from elspeth.web.sessions.converters import state_from_record


def authored_state_hash(state: CompositionState) -> str:
    payload = state.to_dict()
    del payload["version"]
    return stable_hash(payload)


def _changed_fields(before: CompositionState, after: CompositionState) -> tuple[str, ...]:
    """Compare owned states without exposing intermediate dynamic projections."""
    projections = [state.to_dict() for state in (before, after)]
    # Stable component names make changed fields meaningful across removals
    # and replacement. Preserve ordering separately instead of losing it.
    for payload in projections:
        del payload["version"]
        for key, identity in (("nodes", "id"), ("edges", "id"), ("outputs", "name")):
            entries = payload[key]
            payload[f"{key}_order"] = [item[identity] for item in entries]
            payload[key] = {item[identity]: item for item in entries}

    def compare(old: object, new: object, prefix: str = "") -> tuple[str, ...]:
        if old == new:
            return ()
        if type(old) is dict and type(new) is dict:
            fields = []
            for key in sorted(old.keys() | new.keys()):
                path = f"{prefix}.{key}" if prefix else key
                if key not in old or key not in new:
                    fields.append(path)
                else:
                    fields.extend(compare(old[key], new[key], path))
            return tuple(fields)
        return (prefix,)

    return compare(projections[0], projections[1])


type DeliveryScope = Literal["primary", "planner"]


@final
@dataclass(frozen=True, slots=True)
class BlobInspectionCoverage:
    """Delivered character ranges for one immutable original-byte identity."""

    blob_id: str
    content_hash: str
    total_chars: int
    ranges: tuple[tuple[int, int], ...]

    @property
    def complete(self) -> bool:
        return self.ranges == ((0, self.total_chars),)


@final
@dataclass(frozen=True, slots=True)
class _RegisteredBlobPage:
    tool_call_id: str
    producer_call_id: str
    envelope_hash: str
    blob_id: str
    content_hash: str
    total_chars: int
    start_char: int
    end_char: int


@final
@dataclass(frozen=True, slots=True)
class BlobDeliveryAttempt:
    """A selected request, staged before dispatch and confirmed after settlement."""

    owner: TurnEvidence = field(repr=False)
    scope: DeliveryScope
    generation: int
    registration_revision: int
    messages_hash: str
    tools_hash: str | None
    previous_call_ids: frozenset[str]
    pages: tuple[_RegisteredBlobPage, ...]


@final
@dataclass(frozen=True, slots=True)
class AdvisorPublicationReview:
    """Derived private END input, bound to one candidate and request snapshot."""

    binding: str
    semantic_repair_key: str
    payload_json: str = field(repr=False)


def _coverage(pages: Sequence[_RegisteredBlobPage]) -> tuple[BlobInspectionCoverage, ...]:
    series: dict[tuple[str, str, int], list[tuple[int, int]]] = {}
    for page in pages:
        key = (page.blob_id, page.content_hash, page.total_chars)
        series.setdefault(key, []).append((page.start_char, page.end_char))
    result = []
    for (blob_id, content_hash, total), ranges in sorted(series.items()):
        merged: list[tuple[int, int]] = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        result.append(BlobInspectionCoverage(blob_id, content_hash, total, tuple(merged)))
    return tuple(result)


@final
@dataclass(frozen=True, slots=True)
class PersistedBatchEffects:
    """One committed batch may contain several tool calls and state revisions."""

    assistant_message_id: str
    state_id: str
    version: int
    tool_call_ids: tuple[str, ...]
    inserted_state_ids: tuple[str, ...]
    authored_state_hash: str

    def __post_init__(self) -> None:
        freeze_fields(self, "tool_call_ids", "inserted_state_ids")


@final
@dataclass(frozen=True, slots=True)
class PersistedTurnEffects:
    initial_state: CompositionState = field(repr=False)
    initial_state_id: str | None
    latest_verified_state: CompositionState = field(repr=False)
    latest_verified_state_id: str | None
    latest_committed_state_id: str | None
    verification: Literal["verified", "unavailable", "not_persisted"]
    initial_authored_state_hash: str
    latest_authored_state_hash: str
    net_changed_fields: tuple[str, ...] = ()
    batches: tuple[PersistedBatchEffects, ...] = ()
    committed_revision_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        freeze_fields(self, "batches", "committed_revision_ids", "net_changed_fields")

    @property
    def saved_revision_count(self) -> int:
        return len(self.committed_revision_ids)

    @property
    def net_changed(self) -> bool | None:
        if self.verification != "verified":
            return None
        return self.initial_authored_state_hash != self.latest_authored_state_hash

    @property
    def changed_fields(self) -> tuple[str, ...] | None:
        if self.verification != "verified":
            return None
        return self.net_changed_fields


@final
class TurnEvidence:
    """One request's private evidence owner; snapshots never cross requests."""

    __slots__ = (
        "_conflicting_page_ids",
        "_current_calls",
        "_current_pages",
        "_delivery_generations",
        "_historical_pages",
        "_pages",
        "_registration_revision",
        "_retained_history",
        "effects",
        "operation_id",
        "request_hash",
        "request_id",
        "session_id",
    )

    def __init__(
        self,
        *,
        session_id: str | None,
        session_operation_context: SessionOperationContext | None,
        request_id: str,
        request: str,
        initial_state_id: str | None,
        initial_state: CompositionState,
        retained_operation_context: RetainedOperationContext | None = None,
        actor_user_id: str | None = None,
    ) -> None:
        if type(initial_state) is not CompositionState or type(request_id) is not str or not request_id:
            raise TypeError("Turn evidence requires an owned initial state and request identity")
        if session_operation_context is not None and (
            type(session_operation_context) is not SessionOperationContext or session_operation_context.fence.session_id != session_id
        ):
            raise AuditIntegrityError("Turn evidence session differs from its actual operation fence")
        self.session_id = session_id
        self.operation_id = session_operation_context.fence.operation_id if session_operation_context is not None else None
        self.request_id = request_id
        self.request_hash = stable_hash(request)
        if retained_operation_context is not None:
            if type(retained_operation_context) is not RetainedOperationContext:
                raise TypeError("Turn evidence requires owned retained operation history")
            retained_operation_context.validate_for(session_id=session_id, actor_user_id=actor_user_id)
        self._retained_history = retained_operation_context.provider_message() if retained_operation_context is not None else None
        # These metadata collections are bounded by the existing request's
        # admitted tool-call/turn limits. They retain no file content.
        self._pages: dict[str, _RegisteredBlobPage] = {}
        self._conflicting_page_ids: set[str] = set()
        self._registration_revision = 0
        self._delivery_generations: dict[DeliveryScope, int] = {"primary": 0, "planner": 0}
        self._current_pages: dict[DeliveryScope, tuple[_RegisteredBlobPage, ...]] = {"primary": (), "planner": ()}
        self._current_calls: dict[DeliveryScope, tuple[str, str] | None] = {"primary": None, "planner": None}
        self._historical_pages: dict[str, _RegisteredBlobPage] = {}
        initial_hash = authored_state_hash(initial_state)
        self.effects = PersistedTurnEffects(
            initial_state=initial_state,
            initial_state_id=initial_state_id,
            latest_verified_state=initial_state,
            latest_verified_state_id=initial_state_id,
            latest_committed_state_id=initial_state_id,
            verification="verified" if session_id is not None and self.operation_id is not None else "not_persisted",
            initial_authored_state_hash=initial_hash,
            latest_authored_state_hash=initial_hash,
        )

    @property
    def identity(self) -> str:
        identity = stable_hash(
            {
                "session_id": self.session_id,
                "operation_id": self.operation_id,
                "request_id": self.request_id,
                "request_hash": self.request_hash,
                "initial_state_id": self.effects.initial_state_id,
                "latest_verified_state_id": self.effects.latest_verified_state_id,
                "latest_committed_state_id": self.effects.latest_committed_state_id,
                "verification": self.effects.verification,
                "saved_revisions": self.effects.committed_revision_ids,
                "authored_state_hash": self.effects.latest_authored_state_hash,
                "candidate_call": self._current_calls["primary"],
                "candidate_inspection": [(page.tool_call_id, page.envelope_hash) for page in self._current_pages["primary"]],
            }
        )
        return (
            stable_hash({"evidence": identity, "retained_history": self._retained_history})
            if self._retained_history is not None
            else identity
        )

    def register_blob_page(
        self,
        *,
        tool_call_id: str,
        admitted: AdmittedDiscoveryResult,
        selected_content: str,
        producer_call_id: str | None,
    ) -> bool:
        """Register admission, without claiming provider delivery or inspection."""
        if type(admitted) is not AdmittedDiscoveryResult:
            raise TypeError("Blob coverage requires a nominal admitted discovery result")
        if admitted.contract is not response_contract_for("get_blob_content") or not admitted.result.success:
            return False
        if admitted.response is None or producer_call_id is None:
            return False
        if selected_content != serialize_admitted_discovery_result(admitted):
            raise AuditIntegrityError("Blob registration differs from the selected result envelope")
        payload = GetBlobContentDataModel.model_validate(admitted.response.to_wire())
        page = _RegisteredBlobPage(
            tool_call_id,
            producer_call_id,
            stable_hash(selected_content),
            payload.blob_id,
            payload.content_hash,
            payload.total_chars,
            payload.start_char,
            payload.end_char,
        )
        if tool_call_id in self._conflicting_page_ids:
            return False
        existing = self._pages.get(tool_call_id)
        if existing is not None:
            if existing == page:
                return True
            del self._pages[tool_call_id]
            self._conflicting_page_ids.add(tool_call_id)
            self._registration_revision += 1
            for scope in ("primary", "planner"):
                self._current_pages[scope] = ()
                self._current_calls[scope] = None
            return False
        self._pages[tool_call_id] = page
        self._registration_revision += 1
        return True

    def prepare_delivery(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        previous_calls: Sequence[ComposerLLMCall],
        tools: Sequence[Mapping[str, Any]] | None,
        scope: DeliveryScope,
    ) -> BlobDeliveryAttempt:
        """Invalidate prior availability even if the next dispatch never succeeds."""
        self._delivery_generations[scope] += 1
        self._current_pages[scope] = ()
        self._current_calls[scope] = None
        selected: dict[str, _RegisteredBlobPage] = {}
        duplicate_ids: set[str] = set()
        seen_ids: set[str] = set()
        for message in messages:
            if message["role"] != "tool" or "tool_call_id" not in message or "content" not in message:
                continue
            tool_id = message["tool_call_id"]
            if type(tool_id) is not str:
                continue
            if tool_id in seen_ids:
                duplicate_ids.add(tool_id)
            seen_ids.add(tool_id)
            page = self._pages.get(tool_id)
            if page is not None and type(message["content"]) is str and stable_hash(message["content"]) == page.envelope_hash:
                selected[tool_id] = page
        return BlobDeliveryAttempt(
            self,
            scope,
            self._delivery_generations[scope],
            self._registration_revision,
            stable_hash(messages),
            stable_hash(tools) if tools is not None else None,
            frozenset(call.call_id for call in previous_calls if call.call_id is not None),
            tuple(page for tool_id, page in selected.items() if tool_id not in duplicate_ids),
        )

    def confirm_delivery(self, attempt: BlobDeliveryAttempt, retained_calls: Sequence[ComposerLLMCall]) -> bool:
        """Caller invokes only after admission AND provider custody settle."""
        if type(attempt) is not BlobDeliveryAttempt or attempt.owner is not self:
            raise TypeError("Delivery attempt belongs to a different request evidence owner")
        if attempt.generation != self._delivery_generations[attempt.scope] or attempt.registration_revision != self._registration_revision:
            return False
        matching = [
            call
            for call in retained_calls
            if type(call) is ComposerLLMCall
            and call.call_id is not None
            and call.call_id not in attempt.previous_call_ids
            and call.status is ComposerLLMCallStatus.SUCCESS
            and call.messages_hash == attempt.messages_hash
            and call.tools_spec_hash == attempt.tools_hash
        ]
        if not matching:
            return False
        call_id = matching[-1].call_id
        assert call_id is not None
        self._current_calls[attempt.scope] = (call_id, attempt.messages_hash)
        self._current_pages[attempt.scope] = attempt.pages
        self._delivery_generations[attempt.scope] += 1
        for page in attempt.pages:
            self._historical_pages[page.tool_call_id] = page
        return True

    def current_call_id(self, scope: DeliveryScope) -> str | None:
        current = self._current_calls[scope]
        return current[0] if current is not None else None

    def inspection_coverage(self, scope: DeliveryScope) -> tuple[BlobInspectionCoverage, ...]:
        return _coverage(self._current_pages[scope])

    def historical_inspection_coverage(self) -> tuple[BlobInspectionCoverage, ...]:
        return _coverage(tuple(self._historical_pages.values()))

    def review_candidate(
        self, *, candidate: str, request: str, current_state: CompositionState, scope: DeliveryScope = "primary"
    ) -> AdvisorPublicationReview:
        if stable_hash(request) != self.request_hash:
            raise AuditIntegrityError("Publication candidate belongs to a different user request")
        if type(current_state) is not CompositionState or type(candidate) is not str:
            raise TypeError("Publication review requires the owned current state and candidate text")
        if scope not in ("primary", "planner"):
            raise TypeError("Publication review requires an owned delivery scope")
        effects = self.effects
        fields = effects.changed_fields
        # A bounded projection exposes omission explicitly. The binding below
        # still covers the complete evidence and complete candidate/request.
        field_limit = 64

        def inspection(scope: DeliveryScope | None) -> list[dict[str, JsonValue]]:
            coverage = self.historical_inspection_coverage() if scope is None else self.inspection_coverage(scope)
            return [
                {
                    "blob_id": item.blob_id,
                    "content_hash": item.content_hash,
                    "total_chars": item.total_chars,
                    "ranges": [[start, end] for start, end in item.ranges],
                    "complete": item.complete,
                }
                for item in coverage
            ]

        payload = {
            "candidate_reply": candidate,
            "current_request": request,
            "current_graph_hash": authored_state_hash(current_state),
            "saved_effects": {
                "verification": effects.verification,
                "saved_revision_count": effects.saved_revision_count,
                "latest_committed_state_id": effects.latest_committed_state_id,
                "latest_verified_state_id": effects.latest_verified_state_id,
                "initial_authored_state_hash": effects.initial_authored_state_hash,
                "latest_verified_authored_state_hash": effects.latest_authored_state_hash,
                "net_changed": effects.net_changed,
                "changed_fields": fields[:field_limit] if fields is not None else None,
                "changed_fields_omitted": max(0, len(fields) - field_limit) if fields is not None else None,
            },
            "inspection": {
                "current_candidate_call": self._current_calls[scope],
                "currently_available": inspection(scope),
                "historically_delivered": inspection(None),
                "semantic_equivalence_established": False,
            },
        }
        if self._retained_history is not None:
            payload.update(json.loads(self._retained_history))
        evidence_identity = self.identity
        if scope == "planner":
            evidence_identity = stable_hash(
                {
                    "request_evidence": evidence_identity,
                    "candidate_scope": scope,
                    "candidate_call": self._current_calls[scope],
                    "candidate_inspection": [(page.tool_call_id, page.envelope_hash) for page in self._current_pages[scope]],
                }
            )
        binding = stable_hash(
            {"candidate": candidate, "request": request, "evidence": evidence_identity, "current_graph": authored_state_hash(current_state)}
        )
        # Repair progress excludes transport identities. Publication permission
        # still requires the exact binding above, including its physical call.
        semantic_repair_key = stable_hash(
            {
                "request_scope": (self.session_id, self.operation_id, self.request_id, self.request_hash),
                "candidate": candidate,
                "request": request,
                "current_graph": authored_state_hash(current_state),
                "saved_effects": {
                    "initial_state_id": effects.initial_state_id,
                    "latest_verified_state_id": effects.latest_verified_state_id,
                    "latest_committed_state_id": effects.latest_committed_state_id,
                    "committed_revision_ids": effects.committed_revision_ids,
                    "verification": effects.verification,
                    "initial_authored_state_hash": effects.initial_authored_state_hash,
                    "latest_verified_authored_state_hash": effects.latest_authored_state_hash,
                    "net_changed": effects.net_changed,
                    "changed_fields": fields,
                },
                "currently_available": inspection(scope),
                # This separate history projection is supplied to the advisor;
                # it never fills a gap in currently available content.
                "historically_delivered": inspection(None),
            }
        )
        if self._retained_history is not None:
            semantic_repair_key = stable_hash({"repair": semantic_repair_key, "retained_history": self._retained_history})
        return AdvisorPublicationReview(
            binding=binding, semantic_repair_key=semantic_repair_key, payload_json=json.dumps(payload, ensure_ascii=False)
        )

    def accept_checkpoint(
        self,
        witness: CommittedCheckpointWitness,
        *,
        expected_state: CompositionState,
        tool_call_ids: tuple[str, ...],
        assistant_message_id: str,
    ) -> None:
        if type(witness) is not CommittedCheckpointWitness or type(expected_state) is not CompositionState:
            raise TypeError("Turn effects require a nominal checkpoint witness and expected state")
        if self.session_id is None or self.operation_id is None:
            raise AuditIntegrityError("A sessionless request cannot claim a committed checkpoint")
        if str(witness.state.session_id) != self.session_id or witness.creator_operation_id != self.operation_id:
            self.effects = replace(self.effects, verification="unavailable")
            raise AuditIntegrityError("Committed checkpoint belongs to another request fence or session")
        inserted = tuple(str(item) for item in witness.inserted_state_ids)
        known = self.effects.committed_revision_ids
        if set(inserted) & set(known):
            self.effects = replace(self.effects, verification="unavailable")
            raise AuditIntegrityError("A committed checkpoint cannot be counted twice")
        # Retain known commit identity before pure comparison can fail.
        self.effects = replace(self.effects, latest_committed_state_id=str(witness.state.id), committed_revision_ids=(*known, *inserted))
        try:
            saved = state_from_record(witness.state)
            saved_hash = authored_state_hash(saved)
            if saved_hash != authored_state_hash(expected_state):
                raise AuditIntegrityError("Committed authored state differs from the dispatched checkpoint")
            # Materialize comparison under P4's owned projection lifetime.
            changed_fields = _changed_fields(self.effects.initial_state, saved)
        except (ValueError, TypeError, AuditIntegrityError) as exc:
            self.effects = replace(self.effects, verification="unavailable")
            if isinstance(exc, AuditIntegrityError):
                raise
            raise AuditIntegrityError("Committed checkpoint could not be decoded or compared") from exc
        batch = PersistedBatchEffects(
            assistant_message_id=assistant_message_id,
            state_id=str(witness.state.id),
            version=witness.state.version,
            tool_call_ids=tool_call_ids,
            inserted_state_ids=inserted,
            authored_state_hash=saved_hash,
        )
        self.effects = replace(
            self.effects,
            latest_verified_state=saved,
            latest_verified_state_id=str(witness.state.id),
            latest_authored_state_hash=saved_hash,
            net_changed_fields=changed_fields,
            verification="verified",
            batches=(*self.effects.batches, batch),
        )

    def render_saved_scope(self) -> str:
        """Public status contains counts only, never private values or field paths."""
        count = self.effects.saved_revision_count
        if self.effects.verification != "verified":
            return (
                f"This request saved {count} draft revisions. " if count else ""
            ) + "Saved-change verification is unavailable; review the current draft."
        if self.effects.net_changed:
            return f"This request saved {count} draft revisions with verified changes. Saved changes do not establish that the requested task is complete."
        if count:
            return f"This request saved {count} draft revisions. The final authored configuration has no net change from the request's starting draft."
        return "No pipeline changes were saved in this request."

    def mark_verification_unavailable(self) -> None:
        self.effects = replace(self.effects, verification="unavailable")
