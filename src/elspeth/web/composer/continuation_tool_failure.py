"""Native failure lifetime and closed portable correspondence, without authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall, _ToolOutcome
from elspeth.web.composer.continuation_codec import PrivateChunk
from elspeth.web.composer.continuation_tool_batch import RetainedAdvisorRefusalSource, RetainedBatchFailure
from elspeth.web.composer.protocol import ComposerPluginCrashError
from elspeth.web.required_work import RequiredWorkBinding
from elspeth.web.sessions.composer_operations import ComposerOperationRunning

if TYPE_CHECKING:
    from elspeth.web.composer.audit import BufferingRecorder, DispatchAudit
    from elspeth.web.composer.continuation_batch_data import RestoredBatchData
    from elspeth.web.composer.provider_quota import ProviderCallCustody
    from elspeth.web.composer.tool_batch import ContinuationBatchState, ContinuationCallState


def combine_failure_objects(original: BaseException, *others: BaseException) -> BaseException:
    def contains(group: BaseException, target: BaseException) -> bool:
        pending = [group]
        seen: set[int] = set()
        while pending:
            item = pending.pop()
            if item is target:
                return True
            if id(item) in seen:
                continue
            seen.add(id(item))
            if isinstance(item, BaseExceptionGroup):
                pending.extend(item.exceptions)
            if item.__cause__ is not None:
                pending.append(item.__cause__)
        return False

    values: list[BaseException] = []
    for item in (original, *others):
        if any(contains(previous, item) for previous in values):
            continue
        covered = [index for index, previous in enumerate(values) if contains(item, previous)]
        if covered:
            position = covered[0]
            values = [previous for index, previous in enumerate(values) if index not in covered]
            values.insert(position, item)
        else:
            values.append(item)
    return values[0] if len(values) == 1 else BaseExceptionGroup("Original tool failure and required custody failures", values)


@dataclass(slots=True)
class ContinuationToolFailureCustody:
    running: ComposerOperationRunning
    work: RequiredWorkBinding
    response: PrivateChunk
    batch: ContinuationBatchState
    calls: tuple[_AdmittedToolCall, ...]
    recorder: BufferingRecorder
    call: ContinuationCallState
    original: BaseException | None = None
    errors: list[BaseException] = field(default_factory=list)
    invocation: ComposerToolInvocation | None = None
    wrapper: ComposerPluginCrashError | None = None
    completed: bool = False
    handled: list[BaseException] = field(default_factory=list)
    observation: ContinuationDispatchFailureObservation = field(init=False)

    def __post_init__(self) -> None:
        if (
            self.work.running is not self.running
            or self.call.tool_call is not self.calls[self.call.index]
            or self.batch.current_call is not self.call
            or self.call.audit is None
        ):
            raise AuditIntegrityError("tool failure custody changed its actual issued owners")
        self.observation = ContinuationDispatchFailureObservation(self, self.call.audit)

    def observe_primary(self, error: BaseException) -> None:
        # Retain the pointer before any capture/validation/serialization work.
        if self.original is None:
            self.original = error
        elif self.original is not error:
            self.observe_secondary(error)

    def observe_secondary(self, error: BaseException) -> None:
        if error is not self.original and not any(error is item for item in self.errors):
            self.errors.append(error)

    def failure(self, error: BaseException) -> BaseException:
        self.observe_secondary(error)
        original = error if self.original is None else self.original
        if self.observation.feedback is not None:
            return combine_failure_objects(self.observation.feedback, original, *self.errors)
        return combine_failure_objects(original, *self.errors)

    def consume_nonprimary(self) -> None:
        """Retain a converted feedback origin without promoting it to terminal."""
        if self.original is not None:
            if self.errors or self.completed:
                raise AuditIntegrityError("nonprimary feedback cannot consume an incomplete primary/audit failure")
            self.handled.append(self.original)
            self.original = None

    def promote_completed_failure(
        self,
        *,
        kind: Literal["plugin_crash", "advisor_failure"],
        original: BaseException,
        refusal: Literal["context_unsupported", "provider_cost_refused", "prompt_guard_refused"] | None = None,
        provider_call_id: str | None = None,
        allowance_limit: int | None = None,
        wrapper: ComposerPluginCrashError | None = None,
    ) -> None:
        self.observe_primary(original)
        if self.completed or self.errors or self.original is not original or isinstance(original, BaseExceptionGroup):
            raise AuditIntegrityError("tool failure promotion lost its one complete native origin")
        invocation = self.observation.stored
        if invocation is None or not self.observation.appended or not self.batch.tool_outcomes:
            raise AuditIntegrityError("tool failure promotion preceded its actual audit and output")
        outcome = self.batch.tool_outcomes[-1]
        if outcome.call is not self.call.tool_call:
            raise AuditIntegrityError("tool failure promotion changed its actual call output")
        if refusal == "context_unsupported":
            allowance = self.batch.manual_advisor_allowance
            if allowance is None or allowance.limit != allowance_limit:
                raise AuditIntegrityError("context refusal lost its original live allowance limit")
            allowance.require_settled(used=allowance.used)
            if not isinstance(outcome.response, Mapping) or outcome.response["budget_used"] != allowance.used:
                raise AuditIntegrityError("context refusal changed its actual settled spent count")
        if kind == "plugin_crash" and not self.observation.execution_failed:
            raise AuditIntegrityError("post-return failure cannot impersonate a thrown tool handler")
        source = None if refusal is None else RetainedAdvisorRefusalSource(refusal, outcome, invocation, provider_call_id, allowance_limit)
        descriptor = RetainedBatchFailure(kind=kind, error_class=type(original).__name__, refusal_source=source)
        _require_failure_output(descriptor, outcome, invocation)
        self.invocation, self.wrapper = invocation, wrapper
        self.batch.retained_failure = descriptor
        self.batch.stop_issued_batch = True
        self.completed = True


@dataclass(slots=True)
class ContinuationDispatchFailureObservation:
    owner: ContinuationToolFailureCustody
    audit: DispatchAudit
    constructed: ComposerToolInvocation | None = None
    stored: ComposerToolInvocation | None = None
    appended: bool = False
    provider_custody: ProviderCallCustody | None = None
    returned: bool = False
    returned_output: object = None
    execution_failed: bool = False
    rebinds: list[DispatchAudit] = field(default_factory=list)
    feedback: BaseException | None = None

    def observe_feedback(self, error: BaseException) -> None:
        if self.feedback is not None:
            raise AuditIntegrityError("dispatch converted more than one argument origin")
        self.feedback = error

    def require(self, recorder: BufferingRecorder, audit: DispatchAudit) -> None:
        if recorder is not self.owner.recorder or audit is not self.audit:
            raise AuditIntegrityError("dispatch failure observation belongs to another actual audit")

    def observe_primary(self, error: BaseException) -> None:
        self.owner.observe_primary(error)

    def observe_execution_failure(self, error: BaseException) -> None:
        self.execution_failed = True
        self.observe_primary(error)

    def observe_returned(self, output: object) -> None:
        if self.returned or self.execution_failed:
            raise AuditIntegrityError("tool execution returned after an already observed outcome")
        self.returned, self.returned_output = True, output

    def require_rebind_open(self, old: DispatchAudit) -> None:
        if (
            old is not self.audit
            or old is not self.owner.call.audit
            or self.constructed is not None
            or self.stored is not None
            or self.returned
            or self.execution_failed
            or self.feedback is not None
            or self.owner.call.phase != "prepared"
            or self.owner.batch.current_call is not self.owner.call
        ):
            raise AuditIntegrityError("dispatch rebind lost its actual open owner")

    def observe_secondary(self, error: BaseException) -> None:
        self.owner.observe_secondary(error)

    def observe_constructed_invocation(self, invocation: ComposerToolInvocation) -> None:
        self.constructed = invocation
        if invocation.tool_call_id != self.audit.tool_call_id or self.stored is not None:
            raise AuditIntegrityError("dispatch failure observation changed its actual invocation")


def _require_failure_output(failure: RetainedBatchFailure, outcome: _ToolOutcome, invocation: ComposerToolInvocation) -> None:
    from elspeth.web.composer.audit_storage import _validated_canonical_mapping_pair, _validated_invocation_arguments

    _validated_invocation_arguments(invocation)
    if invocation.result_canonical is not None:
        if invocation.result_hash is None:
            raise AuditIntegrityError("retained failure invocation lost its original result hash")
        _validated_canonical_mapping_pair(invocation.result_canonical, invocation.result_hash, label="retained failure original result")
    if (
        outcome.call.id != invocation.tool_call_id
        or outcome.call.function.name != invocation.tool_name
        or outcome.pre_version != invocation.version_before
        or outcome.strict_sent is not invocation.strict_sent
        or outcome.wire_conformant is not invocation.wire_conformant
        or invocation.private_authoring is None
    ):
        raise AuditIntegrityError("retained failure changed its original call/authoring/wire")
    if invocation.private_authoring.raw_arguments != outcome.call.function.arguments:
        raise AuditIntegrityError("retained failure changed its actual received private authoring")
    refusal = failure.refusal_source
    if (failure.kind == "advisor_failure") is not (outcome.call.function.name == "request_advisor_hint"):
        raise AuditIntegrityError("retained primary failure changed its actual plugin/Advisor call family")
    if refusal is None:
        if (
            invocation.status is not ComposerToolStatus.PLUGIN_CRASH
            or invocation.error_class != failure.error_class
            or invocation.private_failure is None
            or invocation.result_canonical is not None
            or invocation.result_hash is not None
            or outcome.response is not None
            or outcome.error_class != failure.error_class
            or outcome.error_category is not None
            or outcome.error_message != failure.error_class
            or invocation.version_after is not None
        ):
            raise AuditIntegrityError("retained crash differs from its actual invocation/private facts")
        return
    if (
        failure.kind != "advisor_failure"
        or refusal.outcome is not outcome
        or refusal.invocation is not invocation
        or invocation.tool_name != "request_advisor_hint"
        or invocation.status is not ComposerToolStatus.SUCCESS
        or invocation.private_failure is not None
        or invocation.error_class is not None
        or outcome.error_class is not None
        or outcome.error_category is not None
        or outcome.error_message is not None
        or invocation.version_after != outcome.post_version
        or outcome.post_version != outcome.pre_version
        or type(outcome.response) not in (dict, MappingProxyType)
        or canonical_json(outcome.response) != invocation.result_canonical
    ):
        raise AuditIntegrityError("retained successful refusal lost its exact outcome/audit aliases")
    payload = outcome.response
    if not isinstance(payload, Mapping):
        raise AuditIntegrityError("retained refusal lost its actual mapping response")
    if payload.get("success") is not False:
        raise AuditIntegrityError("retained Advisor refusal changed its actual unsuccessful result")
    if refusal.kind == "context_unsupported":
        from elspeth.web.composer.protocol import _CONTEXT_UNSUPPORTED_DETAILS

        if (
            set(payload)
            != {"success", "outbound_call_made", "error_type", "failure_code", "reason", "detail", "budget_used", "budget_remaining"}
            or failure.error_class != "ComposerContextUnsupported"
            or payload.get("error_type") != "composer_context_unsupported"
            or payload.get("failure_code") != "context_unsupported"
            or type(payload.get("reason")) is not str
            or payload["reason"] not in _CONTEXT_UNSUPPORTED_DETAILS
            or payload.get("detail") != _CONTEXT_UNSUPPORTED_DETAILS[payload["reason"]]
            or payload.get("outbound_call_made") is not False
        ):
            raise AuditIntegrityError("retained context refusal changed its closed original facts")
    elif refusal.kind == "provider_cost_refused":
        if (
            set(payload) != {"success", "status", "code", "outbound_call_made"}
            or failure.error_class != "CommonProviderCostRefused"
            or payload.get("status") != "PROVIDER_POLICY_REFUSED"
            or type(payload.get("code")) is not str
            or payload.get("outbound_call_made") is not (refusal.provider_call_id is not None)
        ):
            raise AuditIntegrityError("retained policy refusal changed its actual cost correlation")
    elif (
        set(payload) != {"success", "outbound_call_made", "error_type", "detail", "audit_id"}
        or failure.error_class != "PromptGuardRefused"
        or payload.get("error_type") not in ("prompt_guard_unsupported_input", "prompt_guard_blocked", "prompt_guard_unavailable")
        or type(payload.get("audit_id")) is not str
        or payload.get("outbound_call_made") is not False
    ):
        raise AuditIntegrityError("retained guard refusal lost its original guard audit")


def require_retained_failure_links(data: RestoredBatchData, invocations: tuple[ComposerToolInvocation, ...] | None = None) -> None:
    batch = data.batch
    failure = batch.retained_failure
    for index, outcome in enumerate(batch.tool_outcomes):
        refused = isinstance(outcome.response, Mapping) and (
            outcome.response.get("error_type")
            in ("composer_context_unsupported", "prompt_guard_unsupported_input", "prompt_guard_blocked", "prompt_guard_unavailable")
            or outcome.response.get("status") == "PROVIDER_POLICY_REFUSED"
        )
        crashed = invocations is not None and any(
            item.tool_call_id == outcome.call.id and item.status is ComposerToolStatus.PLUGIN_CRASH for item in invocations
        )
        if (refused or crashed) and (failure is None or failure.kind == "advisor_timeout" or index != len(batch.tool_outcomes) - 1):
            raise AuditIntegrityError("primary failed output lost its final retained failure/prefix disposition")
    if failure is None:
        return
    if failure.kind == "advisor_timeout":
        if failure.refusal_source is not None:
            raise AuditIntegrityError("Advisor timeout cannot carry primary refusal custody")
        return
    if not batch.stop_issued_batch or not batch.tool_outcomes or batch.next_tool_index != len(batch.tool_outcomes):
        raise AuditIntegrityError("retained failure lost its actual stopped completed prefix")
    outcome = batch.tool_outcomes[-1]
    if (failure.kind == "advisor_failure") is not (outcome.call.function.name == "request_advisor_hint"):
        raise AuditIntegrityError("retained primary failure changed its actual plugin/Advisor call family")
    if outcome.call is not data.calls[batch.next_tool_index - 1]:
        raise AuditIntegrityError("retained failure changed its exact final admitted call")
    source = failure.refusal_source
    if source is not None and source.invocation.private_authoring is not batch.private_authorings_by_call[outcome.call.id]:
        raise AuditIntegrityError("retained refusal changed its actual original authoring alias")
    if source is not None and source.kind == "context_unsupported":
        response = source.outcome.response
        if not isinstance(response, Mapping) or source.allowance_limit is None:
            raise AuditIntegrityError("retained Advisor context refusal lost its actual allowance")
        if (
            type(response["budget_used"]) is not int
            or type(response["budget_remaining"]) is not int
            or response["budget_used"] != batch.advisor_calls_used
            or response["budget_remaining"] < 0
            or response["budget_remaining"] != source.allowance_limit - batch.advisor_calls_used
        ):
            raise AuditIntegrityError("retained context refusal changed its actual Advisor allowance counts")
    if invocations is None:
        if source is not None:
            _require_failure_output(failure, outcome, source.invocation)
        elif outcome.response is not None or outcome.error_class != failure.error_class:
            raise AuditIntegrityError("retained crash lost its failed completed output")
        return
    matches = tuple(item for item in invocations if item.tool_call_id == outcome.call.id)
    if len(matches) != 1:
        raise AuditIntegrityError("retained failure lost its unique original invocation")
    if matches[0].private_authoring is not batch.private_authorings_by_call[outcome.call.id]:
        raise AuditIntegrityError("retained failure changed its actual original authoring alias")
    _require_failure_output(failure, outcome, matches[0])
