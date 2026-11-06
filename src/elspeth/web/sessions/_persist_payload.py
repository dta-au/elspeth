"""Dataclasses passed across the async/sync boundary in
``SessionServiceImpl.persist_compose_turn`` (spec §5.2.1).

These types have no async behaviour; they are pure data containers that
the compose loop populates in async land and then hands to the sync
worker via ``_run_sync``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, final
from uuid import UUID

from elspeth.contracts.composer_mutation import ComposerMutationRevocation
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import freeze_fields
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.sessions.protocol import ChatMessageRole, CompositionStateData, CompositionStateRecord


@dataclass(frozen=True, slots=True)
class StatePayload:
    """Snapshot of a CompositionState ready for insertion.

    Composes the existing :class:`CompositionStateData` input DTO (which
    carries the per-column state contents and is already
    ``freeze_fields``-protected by its own ``__post_init__``) with a
    ``derived_from_state_id`` that records lineage from the pre-call
    state.

    **B1 (Phase 1 plan-review synthesis): no ``version`` field.** Earlier
    drafts of this dataclass carried a caller-supplied ``version: int``.
    That contract was unsafe: in Phase 3 the compose loop reads
    ``MAX(version)`` outside the session write lock and then
    dispatches into the ``_session_write_lock``-protected ``_insert_composition_state``
    helper. Two concurrent allocators can both compute ``MAX+1`` before
    either acquires the lock; the loser's INSERT then hits
    ``uq_composition_state_version`` and the locked path's
    ``IntegrityError`` handler classifies it as a Tier-1 audit-integrity
    violation — fabricating a Tier-1 violation from a benign contention
    loss. SLO threshold for ``tool_row_integrity_violation_total`` is 0,
    so the alert fires on a non-event. Under ELSPETH's auditability
    standard this is evidence-tampering-class harm: the audit trail
    asserts a violation that did not occur.

    The fix is structural: the version is no longer a payload field at
    all. ``_insert_composition_state`` allocates it under the held
    session write lock via ``SELECT COALESCE(MAX(version), 0) + 1 FROM
    composition_states WHERE session_id = :sid`` (see Task 10). With
    version off the payload, the dual-allocator race becomes
    structurally impossible: every caller must be inside the lock context to invoke
    the helper, and the SELECT-MAX-then-INSERT sequence is atomic
    against every other writer for that session.

    The contract is fixed in Phase 1 even though the call shape only
    manifests in Phase 3 — making the helper impossible to misuse from
    Phase 3 onward is cheaper than catching the misuse later.

    Why this shape rather than a single JSON blob:

    The ``composition_states`` table has eight content columns
    (``source/nodes/edges/outputs/metadata_/is_valid/validation_errors/derived_from_state_id``).
    The plan's earlier ``payload_json: str`` design was a hallucination —
    no ``payload`` column exists, and the existing
    ``save_composition_state`` insert at ``service.py:1005-1020``
    (function body starts at 948) writes
    each column individually via a method-local ``_enveloped(...)`` helper
    and ``deep_thaw(...)`` patterns. Task 10 extracts that rule to the
    shared ``_enveloped_state_column(...)`` helper. ``StatePayload``
    mirrors that real schema by reusing :class:`CompositionStateData` rather than
    duplicating its fields and freeze-guard machinery.

    ``derived_from_state_id`` is ``str | None`` rather than ``str``
    because the existing inline inserts at ``service.py:1005-1020``
    (initial state) currently set it to ``None``. The compose-loop
    caller in Phase 3 will always supply a non-None value (every
    tool-call-driven state advance has a predecessor).

    Note on freeze-fields. ``derived_from_state_id`` is a scalar (or
    ``None``); ``data`` is a frozen ``CompositionStateData`` with its own
    ``freeze_fields`` discipline. No ``__post_init__`` is required on
    ``StatePayload`` itself — ``frozen=True`` is sufficient because
    every remaining field is either scalar or an already-frozen
    dataclass. (Removing ``version`` did not change this analysis;
    ``version: int`` was scalar too.)
    """

    data: CompositionStateData
    derived_from_state_id: str | None = None


@dataclass(frozen=True, slots=True)
class AuditMessageDraft:
    """One row of an audit cohort for ``SessionServiceImpl.add_messages_atomic``.

    A cohort is one logical unit of audit evidence (the LLM-call sidecars
    of one compose request, the tool-invocation breadcrumbs of one turn,
    one planner evidence set) that must become durable together or not at
    all (elspeth-90231248dc). Rows share the caller-supplied
    ``writer_principal``; per-row fields are only the ones that
    legitimately vary inside one cohort.

    ``composition_state_id``, when set, overrides the cohort-level
    ``composition_state_id`` parameter of ``add_messages_atomic`` for
    this row; ``None`` falls back to the cohort-level value. One turn's
    tool rows carry the post-compose state id while its LLM sidecars
    carry the pre-send state id, and forcing those into separate
    transactions to express that difference is exactly the partial-cohort
    defect elspeth-90231248dc removed — the override lets the whole turn
    settle as one cohort.

    ``tool_call_id`` / ``parent_assistant_id`` must satisfy the same
    role biconditional CHECKs as ``add_message``: set exactly when
    ``role="tool"``, ``None`` otherwise.
    """

    role: ChatMessageRole
    content: str
    tool_calls: tuple[Mapping[str, Any], ...] | None = None
    tool_call_id: str | None = None
    parent_assistant_id: str | None = None
    composition_state_id: str | None = None
    inherit_composition_state: bool = True

    def __post_init__(self) -> None:
        if type(self.inherit_composition_state) is not bool or (
            not self.inherit_composition_state and self.composition_state_id is not None
        ):
            raise TypeError("explicit unbound audit state requires an exact flag and no state override")
        # ``tool_calls`` carries audit envelopes that must not change between
        # cohort construction and the atomic insert. Both readers already
        # expect frozen shapes: ``add_messages_atomic`` ``deep_thaw``s before
        # the DB write, and ``record_settled_composer_audit_message`` admits
        # ``MappingProxyType`` alongside ``dict``. ``None`` passes through
        # ``deep_freeze`` unchanged.
        freeze_fields(self, "tool_calls")

    def effective_composition_state_id(self, cohort_state_id: str | None) -> str | None:
        if not self.inherit_composition_state:
            return None
        return self.composition_state_id if self.composition_state_id is not None else cohort_state_id


@dataclass(frozen=True, slots=True)
class RedactedToolRow:
    """One persisted tool row, with redactions already applied."""

    tool_call_id: str
    content: str  # JSON-serialised redacted response
    composition_state_payload: StatePayload | None  # set iff state advanced
    visible_event: VisibleToolEvent | None = None


@dataclass(frozen=True, slots=True)
class RejectionRecord:
    """One refused mutation's durable reason (elspeth-3e28029d2f).

    UNREDACTED by design — operator ruling 2026-09-02: the reason a composer
    mutation tool refused a payload persists as session data (the private
    audit-attribution surface, like ``chat_messages.raw_content``), while the
    public ``tool`` chat row stays redacted. ``planner_payload`` is the exact
    serialized response the planner saw — the text and the reasoning.
    ``tool_call_id`` must name one of the turn's persisted tool rows.
    """

    tool_call_id: str
    tool_name: str
    error_code: str | None  # first coded validation entry, else failure class
    message: str  # extracted human-readable reason
    planner_payload: str  # JSON-serialised unredacted response


@dataclass(frozen=True, slots=True)
class AuditOutcome:
    """Disposition returned by SessionServiceImpl.persist_compose_turn (§5.2.2).

    Two outcome shapes:

    - **Success.** ``assistant_id`` is set, ``unwind_audit_failed=False``.
      Caller continues with the new assistant message id.
    - **Tool failed AND audit unwind failed.** ``assistant_id=None``,
      ``unwind_audit_failed=True``. Caller raises the captured plugin
      crash; the audit failure is recorded by ``persist_compose_turn``
      via counter increment + ``slog.warning`` (permitted under the
      logging-telemetry-policy skill §Logging Policy because the audit
      system itself failed).

    There is NO tier-1-violation outcome shape. When the audit
    database fails AND no plugin crash is in flight,
    ``persist_compose_turn`` raises
    :class:`elspeth.contracts.errors.AuditIntegrityError` directly,
    chained from the underlying ``OperationalError`` via ``raise ...
    from audit_exc``. The exception is registered in
    ``TIER_1_ERRORS`` (via the ``@tier_1_error`` decoration on
    ``AuditIntegrityError``) so ``except Exception:`` blocks cannot
    silently swallow it. The caller has no opportunity to ignore the
    failure — this is the Tier-1 crash doctrine ("Bad data in the
    audit trail = crash immediately") encoded structurally rather
    than asked nicely.

    Why ``unwind_audit_failed`` stays a flag-return (not a raise):
    when a tool plugin has crashed in flight, the caller already has
    a captured plugin-crash exception to raise. Surfacing a separate
    audit exception would mask the original tool failure. The flag
    tells the caller "your raise should ALSO record this audit
    failure," and the counter + slog inside ``persist_compose_turn``
    have already done so.

    Closes synthesised review finding H1 (audit primacy via
    return-flag instead of raised exception violates Tier-1 doctrine).
    """

    assistant_id: str | None
    unwind_audit_failed: bool
    checkpoint_disposition: Literal["committed", "no_new_checkpoint", "empty", "unavailable"]
    checkpoint: CommittedCheckpointWitness | None
    current_state_id: str | None = None
    authority_revocation: ComposerMutationRevocation | None = None
    published_tool_rows: tuple[RedactedToolRow, ...] = ()
    published_assistant_content: str | None = None
    # Only the unavailable continuation receipt retry can populate this.
    # The original plugin crash stays primary; this is its retained secondary.
    continuation_retention_failure: AuditIntegrityError | None = None

    def __post_init__(self) -> None:
        if self.continuation_retention_failure is not None and (
            type(self.continuation_retention_failure) is not AuditIntegrityError or not self.unwind_audit_failed
        ):
            raise ValueError("P4 receipt retention failure requires an unavailable actual outcome")
        if self.authority_revocation is not None:
            if type(self.authority_revocation) is not ComposerMutationRevocation:
                raise TypeError("P4 refusal requires an owned revocation")
            if self.checkpoint_disposition == "committed" or self.checkpoint is not None:
                raise ValueError("Refused graph cannot have a new committed checkpoint")
        if self.assistant_id is None and (self.published_tool_rows or self.published_assistant_content is not None):
            raise ValueError("Rolled-back P4 cannot expose published rows")
        # Success and unwind-failure are the only two valid shapes.
        # Reject any combination that would make the outcome ambiguous.
        if self.assistant_id is not None and self.unwind_audit_failed:
            raise ValueError(
                "AuditOutcome: unwind_audit_failed=True is incompatible with "
                "assistant_id being set; the unwind path cannot have produced "
                "an assistant id"
            )
        if self.unwind_audit_failed:
            if self.checkpoint_disposition != "unavailable" or self.checkpoint is not None:
                raise ValueError("Failed audit unwind cannot carry a committed checkpoint witness")
        elif self.checkpoint_disposition == "committed":
            if type(self.checkpoint) is not CommittedCheckpointWitness or str(self.checkpoint.state.id) != self.current_state_id:
                raise ValueError("Committed audit outcome requires its exact checkpoint witness")
        elif self.checkpoint_disposition == "no_new_checkpoint":
            if self.current_state_id is None or self.checkpoint is not None:
                raise ValueError("Inherited state cannot be claimed as a new checkpoint")
        elif self.checkpoint_disposition == "empty":
            if self.current_state_id is not None or self.checkpoint is not None:
                raise ValueError("Empty checkpoint disposition cannot carry a saved state")
        else:
            raise ValueError("Successful audit outcome requires an explicit checkpoint disposition")


@final
@dataclass(frozen=True, slots=True)
class CommittedCheckpointWitness:
    """Exact inserted row selected in its transaction, returned only after commit.

    This is internal saved-revision evidence, not a later observation of the
    current head. All inserted IDs belong to this batch; the decoded row names
    the last one. Provider tool calls need not map one-to-one to revisions.
    """

    state: CompositionStateRecord
    creator_operation_id: str
    inserted_state_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if type(self.state) is not CompositionStateRecord:
            raise TypeError("Checkpoint witness requires an owned decoded state record")
        if type(self.creator_operation_id) is not str or not self.creator_operation_id:
            raise ValueError("Checkpoint witness requires its actual creator fence identity")
        if (
            type(self.inserted_state_ids) is not tuple
            or not self.inserted_state_ids
            or any(type(item) is not UUID for item in self.inserted_state_ids)
        ):
            raise ValueError("Checkpoint witness requires actual inserted revision identities")
        if self.inserted_state_ids[-1] != self.state.id or len(set(self.inserted_state_ids)) != len(self.inserted_state_ids):
            raise ValueError("Checkpoint witness must identify the final distinct inserted revision")
        freeze_fields(self, "inserted_state_ids")
