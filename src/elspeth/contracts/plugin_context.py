"""Plugin execution context.

The PluginContext carries everything a plugin needs during execution:
- Run metadata (run_id, config)
- Audit trail recording (landscape)
- External call recording (record_call, record_validation_error, record_transform_error)
- Batch transform support (checkpoints, token identity)
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from elspeth.contracts.audit import TokenRef
from elspeth.contracts.call_data import RawCallPayload
from elspeth.contracts.contexts import RateLimitRegistryProtocol
from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken
from elspeth.contracts.enums import CallType as CallTypeEnum
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.events import TelemetryEvent
from elspeth.contracts.freeze import deep_freeze
from elspeth.contracts.node_state_context import AggregationBatchContext
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.contracts.token_usage import TokenUsage
from elspeth.contracts.trust_boundary import observation_boundary

if TYPE_CHECKING:
    from elspeth.contracts import Call, CallStatus, CallType, TransformErrorReason
    from elspeth.contracts.audit_protocols import PluginAuditWriter
    from elspeth.contracts.config.runtime import RuntimeConcurrencyConfig
    from elspeth.contracts.errors import ContractViolation
    from elspeth.contracts.identity import TokenInfo
    from elspeth.contracts.payload_store import PayloadStore
    from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
    from elspeth.contracts.token_usage import TokenUsage

logger = logging.getLogger(__name__)


@observation_boundary(
    tier=3,
    source="optional usage metadata in the external LLM response supplied by a plugin, before audit read-back",
    source_param="response_data",
    suppresses=("R1",),
    invariant=(
        "Missing or malformed usage is observed as unknown by TokenUsage.from_dict; "
        "returns None when no valid usage field exists, preserving partial known counts without inventing zero counts. "
        "The caller records the original response independently; this helper projects audit and telemetry metadata."
    ),
)
def _observed_response_token_usage(response_data: Mapping[str, object]) -> TokenUsage | None:
    """Project optional provider usage without changing the raw response."""
    from elspeth.contracts.token_usage import TokenUsage

    usage = TokenUsage.from_dict(response_data.get("usage"))
    return usage if usage.has_data else None


@dataclass(frozen=True, slots=True)
class ValidationErrorToken:
    """Token returned when recording a validation error.

    Allows tracking the quarantined row through the audit trail.
    Frozen because these are Tier 1 audit records — immutable after creation.
    """

    row_id: str
    node_id: str
    error_id: str | None = None  # Set if recorded to landscape
    destination: str = "discard"  # Sink name or "discard"


@dataclass(frozen=True, slots=True)
class TransformErrorToken:
    """Token returned when recording a transform error.

    Allows tracking the errored row through the audit trail.
    This is for LEGITIMATE processing errors, not transform bugs.
    Frozen because these are Tier 1 audit records — immutable after creation.
    """

    token_id: str
    transform_id: str
    error_id: str | None = None  # Set if recorded to landscape
    destination: str = "discard"  # Sink name or "discard"


@dataclass(init=False)
class PluginContext:
    """Context passed to every plugin operation.

    Provides access to:
    - Run metadata (run_id, config)
    - Audit trail (landscape)
    - External call recording (record_call)
    - Validation/transform error recording
    - Batch checkpoint management

    Example:
        def process(self, row: PipelineRow, ctx: PluginContext) -> TransformResult:
            result = do_work(row, ctx.config)
            return TransformResult.success(result, success_reason={"action": "processed"})
    """

    run_id: str
    _config: Mapping[str, Any] = field(repr=False)

    # === Audit & Infrastructure ===
    landscape: PluginAuditWriter | None = None
    # Authority travels by value from the executor (ADR-048). Leaders also
    # have membership; followers carry only their admitted membership.
    # Writer-less contexts support inspection; audit methods require the
    # concrete authority for their scope and never silently skip a write.
    coordination_token: CoordinationToken | None = None
    member_token: WorkerMembershipToken | None = None
    work_item: TokenWorkItem | None = None
    payload_store: PayloadStore | None = None
    rate_limit_registry: RateLimitRegistryProtocol | None = None
    concurrency_config: RuntimeConcurrencyConfig | None = None
    shutdown_event: threading.Event | None = None

    # Additional metadata
    node_id: str | None = field(default=None)

    # === Row-Level Pipelining (BatchTransformMixin) ===
    # Set by orchestrator/executor when calling accept() on batch transforms.
    # Used by RowReorderBuffer for FIFO ordering and audit attribution.
    # IMPORTANT: This is derivative state - the executor must keep it synchronized
    # with the authoritative token flowing through the pipeline.
    token: TokenInfo | None = field(default=None)

    # === Batch Token Identity (Aggregation) ===
    # Set by AggregationExecutor.execute_flush() before calling batch-aware transforms.
    # Maps row index in the batch to the originating token_id. Batch transforms
    # use this to pass per-row token_id to audited clients for correct telemetry
    # attribution. When None, the transform falls back to ctx.token (single-token mode).
    batch_token_ids: tuple[str, ...] | None = field(default=None)

    # === Aggregation Batch Context ===
    # Set by AggregationExecutor.execute_flush() before calling batch-aware transforms.
    # Carries durable pagination metadata (flush_index, row_start/end, rows_seen_total,
    # is_end_of_source) sourced from AggregationExecutor's checkpointed counters.
    # Cleared by execute_flush() in both the success and failure cleanup paths so
    # stale state never leaks into the next call on the shared context.
    aggregation_batch: AggregationBatchContext | None = field(default=None)

    # === Schema Contract ===
    # Set by executor when processing transforms to enable contract-aware template
    # access (original header names). When transforms receive a plain dict (not
    # PipelineRow), they can still access the contract via ctx.contract.
    # This allows templates using {{ row["Original Header"] }} to resolve correctly.
    contract: SchemaContract | None = field(default=None)

    # === State & Call Recording ===
    # Set by executor to enable transforms to record external calls
    # Exactly one of state_id or operation_id should be set when recording calls
    state_id: str | None = field(default=None)  # For transform calls (via node_states)
    operation_id: str | None = field(default=None)  # For source/sink calls (via operations)
    # Note: call_index allocation is delegated to PluginAuditWriter.allocate_call_index()
    # to ensure coordination with audited clients.

    # === Telemetry Callback ===
    # Callback to emit telemetry events for external calls.
    # Always present - when telemetry is disabled, orchestrator sets this to a no-op.
    # Plugins ALWAYS call this after successful Landscape recording - no None checks.
    telemetry_emit: Callable[[TelemetryEvent], None] = field(default=lambda event: None)

    # Validation errors that must later be linked to a persisted quarantine row.
    # Entries are (match_key, error_id), where match_key hashes the raw row payload
    # before orchestrator normalization/wrapping.
    _pending_quarantine_validation_errors: list[tuple[str, str]] = field(default_factory=list)

    def __init__(
        self,
        run_id: str,
        config: Mapping[str, Any] | None = None,
        landscape: PluginAuditWriter | None = None,
        payload_store: PayloadStore | None = None,
        rate_limit_registry: RateLimitRegistryProtocol | None = None,
        concurrency_config: RuntimeConcurrencyConfig | None = None,
        shutdown_event: threading.Event | None = None,
        node_id: str | None = None,
        token: TokenInfo | None = None,
        batch_token_ids: tuple[str, ...] | None = None,
        aggregation_batch: AggregationBatchContext | None = None,
        contract: SchemaContract | None = None,
        state_id: str | None = None,
        operation_id: str | None = None,
        telemetry_emit: Callable[[TelemetryEvent], None] | None = None,
        coordination_token: CoordinationToken | None = None,
        member_token: WorkerMembershipToken | None = None,
        work_item: TokenWorkItem | None = None,
        _pending_quarantine_validation_errors: list[tuple[str, str]] | None = None,
        _config: Mapping[str, Any] | None = None,
    ) -> None:
        if config is not None and _config is not None:
            raise TypeError("PluginContext accepts either config or _config, not both")
        raw_config = config if config is not None else _config
        if raw_config is None:
            raise TypeError("PluginContext missing required argument: 'config'")

        self.run_id = run_id
        # Deep-freeze config so plugins cannot mutate the run configuration
        # after the audit snapshot (settings_json, config_hash) is recorded.
        # PluginContext is not frozen (checkpoint/token need mutation), but
        # config must be immutable for audit integrity.
        self._config = deep_freeze(raw_config)
        self.landscape = landscape
        self.coordination_token = coordination_token
        if coordination_token is not None and type(coordination_token) is not CoordinationToken:
            raise TypeError("coordination_token must be a CoordinationToken")
        if member_token is not None and type(member_token) is not WorkerMembershipToken:
            raise TypeError("member_token must be a WorkerMembershipToken")
        if coordination_token is not None:
            if member_token is not None and member_token != coordination_token.membership:
                raise FrameworkBugError("PluginContext leader and member identities disagree")
            member_token = coordination_token.membership
        if member_token is not None and member_token.run_id != run_id:
            raise FrameworkBugError("PluginContext membership belongs to a different run")
        self.member_token = member_token
        self.work_item = work_item
        self.payload_store = payload_store
        self.rate_limit_registry = rate_limit_registry
        self.concurrency_config = concurrency_config
        self.shutdown_event = shutdown_event
        self.node_id = node_id
        self.token = token
        self.batch_token_ids = batch_token_ids
        self.aggregation_batch = aggregation_batch
        self.contract = contract
        self.state_id = state_id
        self.operation_id = operation_id
        self.telemetry_emit = telemetry_emit if telemetry_emit is not None else (lambda event: None)
        self._pending_quarantine_validation_errors = (
            [] if _pending_quarantine_validation_errors is None else _pending_quarantine_validation_errors
        )

    @property
    def config(self) -> Mapping[str, Any]:
        """Frozen run configuration exposed read-only for audit integrity."""
        return self._config

    def for_contract(self, contract: SchemaContract | None) -> PluginContext:
        """Return an operation-scoped copy with a different row contract."""
        return PluginContext(
            run_id=self.run_id,
            _config=self._config,
            landscape=self.landscape,
            payload_store=self.payload_store,
            rate_limit_registry=self.rate_limit_registry,
            concurrency_config=self.concurrency_config,
            shutdown_event=self.shutdown_event,
            node_id=self.node_id,
            token=self.token,
            batch_token_ids=self.batch_token_ids,
            aggregation_batch=self.aggregation_batch,
            contract=contract,
            state_id=self.state_id,
            operation_id=self.operation_id,
            telemetry_emit=self.telemetry_emit,
            coordination_token=self.coordination_token,
            member_token=self.member_token,
            work_item=self.work_item,
            _pending_quarantine_validation_errors=self._pending_quarantine_validation_errors,
        )

    def require_coordination_token(self) -> CoordinationToken:
        """Return the executor's leader authority, refusing a read-only context."""
        if not isinstance(self.coordination_token, CoordinationToken):
            raise FrameworkBugError("Plugin audit write requires the executor's leader token")
        return self.coordination_token

    def require_member_token(self) -> WorkerMembershipToken:
        """Return the worker's admitted authority without acquiring or inventing it."""
        if not isinstance(self.member_token, WorkerMembershipToken):
            raise FrameworkBugError("Plugin audit write requires the executor's member token")
        return self.member_token

    def require_work_item(self) -> TokenWorkItem:
        """Return the actual scheduler claim bound to this plugin invocation."""
        if not isinstance(self.work_item, TokenWorkItem):
            raise FrameworkBugError("Row audit write requires the executor's claimed work item")
        return self.work_item

    def record_readiness_check(
        self,
        *,
        name: str,
        collection: str,
        reachable: bool,
        count: int | None,
        message: str,
    ) -> None:
        """Record provider readiness under the worker's admitted membership.

        The token is forwarded by value from the executor that built this
        context; a context without one cannot write, and that is the intended
        failure mode — never a silently skipped audit row.
        """
        from elspeth.contracts import FrameworkBugError

        if self.landscape is None or self.member_token is None:
            raise FrameworkBugError(
                f"record_readiness_check() called without landscape or member token. "
                f"Context state: run_id={self.run_id}, node_id={self.node_id}, "
                f"landscape={'set' if self.landscape is not None else 'None'}, "
                f"member_token={'set' if self.member_token is not None else 'None'}. "
                f"This is a framework bug — the executor must inject both before plugin on_start()."
            )
        self.landscape.record_readiness_check(
            name=name,
            collection=collection,
            reachable=reachable,
            count=count,
            message=message,
            member_token=self.require_member_token(),
        )

    @staticmethod
    def _validation_error_match_key(row: Any) -> str:
        """Build a stable lookup key for a raw validation-error payload."""
        from elspeth.contracts.hashing import repr_hash, stable_hash

        try:
            return stable_hash(row)
        except (ValueError, TypeError):
            return repr_hash(row)

    def pop_pending_quarantine_validation_error_id(self, row: Any) -> str | None:
        """Consume the queued validation error ID matching a quarantined row payload."""
        match_key = self._validation_error_match_key(row)
        for index, (pending_match_key, error_id) in enumerate(self._pending_quarantine_validation_errors):
            if pending_match_key == match_key:
                del self._pending_quarantine_validation_errors[index]
                return error_id
        return None

    def record_call(
        self,
        call_type: CallType,
        status: CallStatus,
        request_data: dict[str, Any],
        response_data: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        latency_ms: float | None = None,
        *,
        provider: str = "unknown",
    ) -> Call | None:
        """Record an external API call to the audit trail and emit telemetry.

        Records source/sink operation calls without manually managing call
        indices. Row calls are recorded by audited clients with their
        member token and claimed work item.

        After recording to Landscape (the legal record), emits an
        ExternalCallCompleted telemetry event for operational visibility.

        Args:
            call_type: Type of call (LLM, HTTP, SQL, FILESYSTEM)
            status: Outcome (SUCCESS, ERROR)
            request_data: Request payload (will be hashed)
            response_data: Response payload (optional for errors)
            error: Error details if status is ERROR
            latency_ms: Call duration in milliseconds
            provider: Provider name for telemetry (e.g., "openrouter", "azure")

        Returns:
            The recorded Call, or None if landscape not configured

        Raises:
            FrameworkBugError: If the context lacks an operation parent or leader authority
        """
        from elspeth.contracts import FrameworkBugError

        if self.landscape is None:
            raise FrameworkBugError(
                f"record_call() called without landscape. "
                f"Context state: run_id={self.run_id}, state_id={self.state_id}, "
                f"operation_id={self.operation_id}. "
                f"This is a framework bug — orchestrator must inject landscape before plugin execution."
            )

        if self.state_id is not None or self.operation_id is None:
            raise FrameworkBugError("PluginContext.record_call requires an operation parent; row clients own row-call recording")
        token_usage = _observed_response_token_usage(response_data) if call_type == CallTypeEnum.LLM and response_data is not None else None
        recorded_call = self.landscape.record_operation_call(
            operation_id=self.operation_id,
            call_type=call_type,
            status=status,
            request_data=RawCallPayload(request_data),
            response_data=RawCallPayload(response_data) if response_data is not None else None,
            error=RawCallPayload(error) if error is not None else None,
            latency_ms=latency_ms,
            coordination_token=self.require_coordination_token(),
            token_usage=token_usage if token_usage is not None else TokenUsage.unknown(),
        )
        parent_id = self.operation_id

        # Emit telemetry AFTER successful Landscape recording
        # Wrapped in try/except to prevent telemetry failures from affecting callers
        try:
            from elspeth.contracts.events import ExternalCallCompleted

            # Pass data directly to RawCallPayload. No defensive copy needed:
            # RawCallPayload.__init__ calls deep_freeze(), which creates an
            # independent frozen copy. Callers mutating the original dict after
            # record_call() won't affect the telemetry payload.
            # (Existing test: test_request_payload_snapshot_is_immutable_after_call)
            request_snapshot = request_data
            response_snapshot = response_data

            # Wrap data in RawCallPayload for typed telemetry payload.
            # RawCallPayload.__init__ calls deep_freeze(), creating an independent
            # frozen copy — no prior snapshot/deepcopy step is needed.
            request_payload = RawCallPayload(request_snapshot)
            response_payload = RawCallPayload(response_snapshot) if response_snapshot is not None else None

            # Use hashes from the recorded Call object — the recorder is the
            # single source of truth for hashing (via core.canonical.stable_hash).
            # Recomputing here would risk divergence if the hash implementations differ.
            self.telemetry_emit(
                ExternalCallCompleted(
                    timestamp=datetime.now(UTC),
                    run_id=self.run_id,
                    # Use correct field based on context type
                    state_id=None,
                    operation_id=self.operation_id,
                    token_id=None,
                    call_type=call_type,
                    provider=provider,
                    status=status,
                    latency_ms=latency_ms,
                    request_hash=recorded_call.request_hash,
                    response_hash=recorded_call.response_hash,
                    request_payload=request_payload,
                    response_payload=response_payload,
                    token_usage=token_usage,
                )
            )
        except (OSError, ConnectionError, TimeoutError) as tel_err:
            # Telemetry transport failures are expected and must not corrupt
            # the call recording. All other exceptions (including Tier 1,
            # programming errors like KeyError) propagate naturally.
            logger.warning(
                "telemetry_emit_failed in record_call",
                extra={
                    "error": str(tel_err),
                    "error_type": type(tel_err).__name__,
                    "run_id": self.run_id,
                    "parent_id": parent_id,
                },
            )

        return recorded_call

    @observation_boundary(
        tier=3,
        source=(
            "the row that failed source validation — external file/API content ELSPETH does not own, "
            "explicitly not required to be a dict (a JSON array of primitives quarantines its "
            "elements here) and not required to be canonically serializable"
        ),
        source_param="row",
        suppresses=("R5",),
        invariant=(
            "never raises on the row: a Mapping carrying 'id' yields that id, anything else is "
            "identified by its canonical hash, and a row that canonical_json rejects (NaN, Infinity, "
            "a non-serializable object) falls back to a repr() hash so the quarantine still gets an "
            "audit row. Recording what was actually seen outranks recording it canonically — the "
            "only failures this method raises on are missing node_id/landscape, which are framework "
            "bugs in the caller, not properties of the row. The landscape writer this delegates to "
            "is non-raising on the same input class by construction: "
            "core/landscape/data_flow/errors.py::record_validation_error routes row_data through "
            "canonical_or_recorded_hash / canonical_or_recorded_json, which return an explicit "
            "repr/NonCanonicalMetadata fallback rather than propagating. Pinned end-to-end against a "
            "real recorder by tests/unit/contracts/test_plugin_context_recording.py::"
            "TestRecordValidationErrorHappyPath::"
            "test_non_canonical_row_does_not_leak_row_content_to_logger"
        ),
    )
    def record_validation_error(
        self,
        row: Any,
        error: str,
        schema_mode: str,
        destination: str,
        *,
        contract_violation: ContractViolation | None = None,
    ) -> ValidationErrorToken:
        """Record a validation error for audit trail.

        Called by sources when row validation fails. The row will be
        quarantined (not processed further) but the error is recorded
        for complete audit coverage.

        Args:
            row: The row data that failed validation (may be non-dict for
                 malformed external data like JSON arrays containing primitives)
            error: Description of the validation failure
            schema_mode: "fixed", "flexible", "observed", or "parse" (parse = file-level parse error)
            destination: Sink name where row is routed, or "discard"
            contract_violation: Optional contract violation details for structured auditing

        Returns:
            ValidationErrorToken for tracking the quarantined row
        """
        from elspeth.contracts.hashing import repr_hash, stable_hash

        # Generate row_id from content hash if not present
        # External data may be non-dict (e.g., JSON array containing primitives),
        # so we must check isinstance before accessing dict keys
        if isinstance(row, dict) and "id" in row:
            row_id = str(row["id"])
        else:
            # Try canonical hash first, fall back to repr() hash for non-serializable data
            # This is Tier-3 (external data) - we must record what we saw, even if malformed
            try:
                row_id = stable_hash(row)[:16]
            except (ValueError, TypeError) as e:
                # Non-canonical data (NaN, Infinity, or other non-serializable types)
                # Hash the repr() instead - not canonical, but preserves audit trail.
                # Log ONLY the error type, never str(e): contracts.hashing
                # canonicalization errors embed `Got: {obj!r}` (raw row content), and
                # row data must stay in Landscape, not cross a normal logging boundary
                # (logging policy; elspeth-05a5727489).
                logger.warning(
                    "Row data not canonically serializable, using repr() hash: %s",
                    type(e).__name__,
                )
                row_id = repr_hash(row)[:16]

        if self.node_id is None:
            from elspeth.contracts import FrameworkBugError

            raise FrameworkBugError(
                f"record_validation_error() called without node_id. "
                f"Context state: run_id={self.run_id}. "
                f"This is a framework bug — orchestrator must set node_id before validation."
            )

        if self.landscape is None:
            from elspeth.contracts import FrameworkBugError

            raise FrameworkBugError(
                f"record_validation_error() called without landscape. "
                f"Context state: run_id={self.run_id}, node_id={self.node_id}. "
                f"This is a framework bug — orchestrator must inject landscape before source validation."
            )

        # Record to landscape audit trail
        error_id = self.landscape.record_validation_error(
            coordination_token=self.require_coordination_token(),
            node_id=self.node_id,
            row_data=row,
            error=error,
            schema_mode=schema_mode,
            destination=destination,
            contract_violation=contract_violation,
        )

        if destination != "discard" and error_id is not None:
            match_key = self._validation_error_match_key(row)
            self._pending_quarantine_validation_errors.append((match_key, error_id))

        return ValidationErrorToken(
            row_id=row_id,
            node_id=self.node_id,
            error_id=error_id,
            destination=destination,
        )

    def record_transform_error(
        self,
        token_id: str,
        transform_id: str,
        row: dict[str, Any] | PipelineRow,
        error_details: TransformErrorReason,
        destination: str,
    ) -> TransformErrorToken:
        """Record a transform processing error for audit trail.

        Called when a transform returns TransformResult.error().
        This is for legitimate errors, NOT transform bugs (which crash).

        Args:
            token_id: Token ID for the row being processed
            transform_id: Transform that returned the error
            row: The row data that could not be processed
            error_details: Error details from TransformResult.error() (TransformErrorReason TypedDict)
            destination: Sink name where row is routed, or "discard"

        Returns:
            TransformErrorToken for tracking
        """
        if self.landscape is None:
            from elspeth.contracts import FrameworkBugError

            raise FrameworkBugError(
                f"record_transform_error() called without landscape. "
                f"Context state: run_id={self.run_id}, node_id={self.node_id}. "
                f"This is a framework bug — orchestrator must inject landscape before transform execution."
            )

        error_id = self.landscape.record_transform_error(
            member_token=self.require_member_token(),
            work_item=self.require_work_item(),
            ref=TokenRef(token_id=token_id, run_id=self.run_id),
            transform_id=transform_id,
            row_data=row,
            error_details=error_details,
            destination=destination,
        )

        return TransformErrorToken(
            token_id=token_id,
            transform_id=transform_id,
            error_id=error_id,
            destination=destination,
        )


class _ContextScopeUnset:
    __slots__ = ()


_CONTEXT_SCOPE_UNSET = _ContextScopeUnset()


@contextmanager
def plugin_context_scope(
    ctx: PluginContext,
    *,
    node_id: str | None | _ContextScopeUnset = _CONTEXT_SCOPE_UNSET,
    token: TokenInfo | None | _ContextScopeUnset = _CONTEXT_SCOPE_UNSET,
    batch_token_ids: tuple[str, ...] | None | _ContextScopeUnset = _CONTEXT_SCOPE_UNSET,
    aggregation_batch: AggregationBatchContext | None | _ContextScopeUnset = _CONTEXT_SCOPE_UNSET,
    contract: SchemaContract | None | _ContextScopeUnset = _CONTEXT_SCOPE_UNSET,
    state_id: str | None | _ContextScopeUnset = _CONTEXT_SCOPE_UNSET,
    operation_id: str | None | _ContextScopeUnset = _CONTEXT_SCOPE_UNSET,
    work_item: TokenWorkItem | None | _ContextScopeUnset = _CONTEXT_SCOPE_UNSET,
) -> Iterator[PluginContext]:
    """Temporarily assign executor-owned metadata on a shared plugin context.

    Executors reuse ``PluginContext`` across operations for infrastructure
    handles and pending audit linkage state. Per-operation metadata must be
    scoped so state/token/contract attribution cannot leak into the next plugin
    call on the same context.
    """
    previous_node_id = ctx.node_id
    previous_token = ctx.token
    previous_batch_token_ids = ctx.batch_token_ids
    previous_aggregation_batch = ctx.aggregation_batch
    previous_contract = ctx.contract
    previous_state_id = ctx.state_id
    previous_operation_id = ctx.operation_id
    previous_work_item = ctx.work_item

    if not isinstance(node_id, _ContextScopeUnset):
        ctx.node_id = node_id
    if not isinstance(token, _ContextScopeUnset):
        ctx.token = token
    if not isinstance(batch_token_ids, _ContextScopeUnset):
        ctx.batch_token_ids = batch_token_ids
    if not isinstance(aggregation_batch, _ContextScopeUnset):
        ctx.aggregation_batch = aggregation_batch
    if not isinstance(contract, _ContextScopeUnset):
        ctx.contract = contract
    if not isinstance(state_id, _ContextScopeUnset):
        ctx.state_id = state_id
    if not isinstance(operation_id, _ContextScopeUnset):
        ctx.operation_id = operation_id
    if not isinstance(work_item, _ContextScopeUnset):
        ctx.work_item = work_item

    try:
        yield ctx
    finally:
        if not isinstance(node_id, _ContextScopeUnset):
            ctx.node_id = previous_node_id
        if not isinstance(token, _ContextScopeUnset):
            ctx.token = previous_token
        if not isinstance(batch_token_ids, _ContextScopeUnset):
            ctx.batch_token_ids = previous_batch_token_ids
        if not isinstance(aggregation_batch, _ContextScopeUnset):
            ctx.aggregation_batch = previous_aggregation_batch
        if not isinstance(contract, _ContextScopeUnset):
            ctx.contract = previous_contract
        if not isinstance(state_id, _ContextScopeUnset):
            ctx.state_id = previous_state_id
        if not isinstance(operation_id, _ContextScopeUnset):
            ctx.operation_id = previous_operation_id
        if not isinstance(work_item, _ContextScopeUnset):
            ctx.work_item = previous_work_item
