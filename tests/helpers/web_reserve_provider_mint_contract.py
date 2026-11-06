"""Finite actual provider owner/mint/caller transfer source contract."""

import ast
import copy

from tests.helpers.web_reserve_proposal_child_transfers import binding_nodes, import_origin, owned_origin, scope_nodes

CONTRACTS = {
    "elspeth.web.composer.provider_quota:_bind_attempt_call": 'def _bind_attempt_call(call: ComposerLLMCall, attempt: ProviderAttempt, admitted_ns: int) -> ComposerLLMCall:\n    """Anchor monotonic call duration to the authoritative database admission.\n\n    The request\'s local wall clock is useful only before quota binding. Both\n    persisted endpoints must share the database clock domain, including when\n    a replica\'s wall clock is ahead of or behind UTC midnight.\n    """\n    if call.call_id == attempt.attempt_id and call.started_at == attempt.started_at:\n        return call\n    elapsed_ns = time.monotonic_ns() - admitted_ns\n    return replace(call, call_id=attempt.attempt_id, started_at=attempt.started_at, finished_at=attempt.started_at + timedelta(microseconds=elapsed_ns // 1000))',
    "elspeth.web.composer.provider_quota:ProviderInvocationFamily": "class ProviderInvocationFamily(IntEnum):\n    PRIMARY = 0\n    ADVISOR = 1\n    TITLE = 3",
    "elspeth.web.composer.provider_quota:ProviderInvocationOwner": "class ProviderInvocationOwner:\n    \"\"\"Single explicit mint owner for independently ordered provider families.\"\"\"\n\n    def __init__(self, *, service: SessionServiceProtocol, required_work: RequiredWorkBinding) -> None:\n        if type(required_work) is not RequiredWorkBinding or required_work.role is not RequiredWorkRole.TURN:\n            raise AuditIntegrityError('Provider invocation owner requires its exact turn binding')\n        required_work.validate_context(required_work.coordinator.authority.context)\n        self._service = service\n        self._required_work = required_work\n        self._counters = dict.fromkeys(ProviderInvocationFamily, 0)\n        self._cost_budget: CommonProviderCostBudget | None = None\n\n    @property\n    def service(self) -> SessionServiceProtocol:\n        return self._service\n\n    @property\n    def required_work(self) -> RequiredWorkBinding:\n        return self._required_work\n\n    @property\n    def cost_budget(self) -> CommonProviderCostBudget | None:\n        return self._cost_budget\n\n    def bind_cost_budget(self, budget: CommonProviderCostBudget) -> None:\n        if type(budget) is not CommonProviderCostBudget:\n            raise AuditIntegrityError('provider owner requires nominal request policy')\n        if self._cost_budget is not None:\n            if self._cost_budget is not budget:\n                raise AuditIntegrityError('provider owner cannot replace request policy')\n            return\n        if self._counters[ProviderInvocationFamily.PRIMARY] or self._counters[ProviderInvocationFamily.ADVISOR]:\n            raise AuditIntegrityError('provider policy cannot start after covered invocation')\n        self._cost_budget = budget\n\n    def mint(self, family: ProviderInvocationFamily) -> ProviderCallCustody:\n        if type(family) is not ProviderInvocationFamily:\n            raise AuditIntegrityError('Provider family is not an owned nominal identity')\n        ordinal = self._counters[family]\n        self._counters[family] = ordinal + 1\n        diagonal = int(family) + ordinal\n        semantic = diagonal * (diagonal + 1) // 2 + ordinal\n        binding = RequiredWorkBinding(self.required_work.coordinator, self.required_work.transition_ordinal, semantic, RequiredWorkRole.TITLE if family is ProviderInvocationFamily.TITLE else RequiredWorkRole.TURN)\n        return ProviderCallCustody(service=self.service, context=binding.coordinator.authority.context, required_work=binding, cost_budget=self.cost_budget if family in (ProviderInvocationFamily.PRIMARY, ProviderInvocationFamily.ADVISOR) else None, family=family)",
    "elspeth.web.composer.provider_quota:ProviderCallCustody": "class ProviderCallCustody:\n    \"\"\"Explicit owned admission, physical dispatch and audit settlement custody.\"\"\"\n    __slots__ = ('__weakref__', '_admitted_ns', '_admitting', '_checkpoint_no_sdk_issuer', '_classifier_no_sdk_issuer', '_context', '_continuation_classifier_v4_finalizer', '_continuation_classifier_v4_terminal', '_continuation_primary_v4_finalizer', '_continuation_primary_v4_sql_committed', '_continuation_primary_v4_terminal', '_continuation_semantic_v4_finalizer', '_continuation_semantic_v4_sql_committed', '_continuation_semantic_v4_terminal', '_cost_budget', '_cost_response_accountable', '_family', '_primary_no_sdk_issuer', '_required_work', '_sdk_entered', '_semantic_no_sdk_issuer', '_service', '_settlements', '_title_no_sdk_issuer', '_undispatched_recurrence', '_v4_returned_incomplete_committed', '_v4_returned_incomplete_finalizer', '_v4_sdk_finalizer', '_v4_sdk_return', '_v4_sdk_return_claimed', 'attempt', 'call', 'continuation_checkpoint_v4', 'continuation_checkpoint_v4_admission', 'continuation_classifier_v4', 'continuation_classifier_v4_admission', 'continuation_classifier_v4_deferred_cancellations', 'continuation_classifier_v4_no_sdk_result', 'continuation_classifier_v4_result', 'continuation_primary_clock', 'continuation_primary_v4', 'continuation_primary_v4_admission', 'continuation_primary_v4_deferred_cancellations', 'continuation_primary_v4_no_sdk_result', 'continuation_primary_v4_result', 'continuation_semantic_v4', 'continuation_semantic_v4_admission', 'continuation_semantic_v4_result', 'continuation_title', 'continuation_title_result', 'continuation_title_selected', 'continuation_title_v4_admission', 'continuation_title_v4_terminal', 'v4_returned_incomplete_result', 'v4_sdk_failure', 'v4_sdk_terminal_failure', 'v4_sdk_terminal_result')\n\n    def __init__(self, *, service: SessionServiceProtocol, context: SessionOperationContext, required_work: RequiredWorkBinding, cost_budget: CommonProviderCostBudget | None=None, family: ProviderInvocationFamily | None=None) -> None:\n        if type(required_work) is not RequiredWorkBinding:\n            raise AuditIntegrityError('Provider custody requires a nominal explicit binding')\n        required_work.validate_context(context)\n        if context.operation_kind is not SessionOperationKind.COMPOSE:\n            raise AuditIntegrityError('Provider custody requires exact COMPOSE authority')\n        if cost_budget is not None and type(cost_budget) is not CommonProviderCostBudget:\n            raise AuditIntegrityError('provider custody received foreign cost policy')\n        if family is not None and type(family) is not ProviderInvocationFamily:\n            raise AuditIntegrityError('Provider custody family requires its owned nominal discriminator')\n        self._family = family\n        self._cost_budget = cost_budget\n        self._cost_response_accountable = False\n        self._service = service\n        self._context = context\n        self._required_work = required_work\n        self._primary_no_sdk_issuer: PrimaryNoSdkIssuer | None = None\n        self._classifier_no_sdk_issuer: ClassifierNoSdkIssuer | None = None\n        self._checkpoint_no_sdk_issuer: CheckpointNoSdkIssuer | None = None\n        self._semantic_no_sdk_issuer: SemanticAdvisorNoSdkIssuer | None = None\n        self._title_no_sdk_issuer: TitleNoSdkIssuer | None = None\n        self.attempt: ProviderAttempt | None = None\n        self.call: ComposerLLMCall | None = None\n        self._sdk_entered = False\n        self.v4_sdk_failure: V4SdkFailureObservation | None = None\n        self._v4_sdk_finalizer: Callable[[V4SdkFailureObservation, RequiredWorkTicket], Awaitable[RequiredSQLFinishOnce[VerifiedSdkTerminal]]] | None = None\n        self.v4_sdk_terminal_result: V4SdkFinalization | None = None\n        self.v4_sdk_terminal_failure: SdkTerminalFailure | None = None\n        self._admitting = False\n        self._admitted_ns = 0\n        self._undispatched_recurrence = 0\n        self._v4_sdk_return: V4ProviderSdkReturn | None = None\n        self._v4_sdk_return_claimed = False\n        self._v4_returned_incomplete_finalizer: Callable[[V4ProviderResultIncomplete, RequiredWorkTicket], Awaitable[RequiredSQLFinishOnce[VerifiedPaidIncompleteTerminal]]] | None = None\n        self._v4_returned_incomplete_committed = False\n        self.v4_returned_incomplete_result: V4ReturnedIncompleteFinalization | None = None\n        self._settlements = 0\n        self.continuation_title: PreparedTitleProvider | None = None\n        self.continuation_primary_clock: ContinuationActionClock | None = None\n        self.continuation_classifier_v4: PreparedClassifierPrimaryProvider | None = None\n        self.continuation_classifier_v4_admission: V4ClassifierProviderAdmission | None = None\n        self.continuation_classifier_v4_result: ClassifierPrimaryResult | VerifiedClassifierPaidOverflowTerminal | None = None\n        self.continuation_classifier_v4_deferred_cancellations: tuple[asyncio.CancelledError, ...] = ()\n        self.continuation_classifier_v4_no_sdk_result: ClassifierNoSdkRefusalReceipt | None = None\n        self.continuation_checkpoint_v4_admission: V4CheckpointProviderAdmission | None = None\n        self.continuation_checkpoint_v4: PreparedCheckpointProvider | None = None\n        self._continuation_classifier_v4_finalizer: Callable[[_CallModelOutcome, RequiredWorkTicket], Awaitable[RequiredSQLFinishOnce[ClassifierPrimaryResult | VerifiedClassifierPaidOverflowTerminal]]] | None = None\n        self._continuation_classifier_v4_terminal = False\n        self.continuation_title_selected: StoredContinuationSnapshot | None = None\n        self.continuation_title_result: TitleAuditSettlementRequest | None = None\n        self.continuation_title_v4_admission: V4TitleProviderAdmission | None = None\n        self.continuation_title_v4_terminal: VerifiedTitlePaidOverflowTerminal | None = None\n        self.continuation_primary_v4: PreparedContinuationProvider | None = None\n        self.continuation_primary_v4_admission: V4PrimaryProviderAdmission | None = None\n        self.continuation_primary_v4_deferred_cancellations: tuple[asyncio.CancelledError, ...] = ()\n        self.continuation_primary_v4_no_sdk_result: PrimaryNoSdkRefusalReceipt | None = None\n        self.continuation_primary_v4_result: PrimaryProviderResult | PrimaryPaidOverflowTerminal | None = None\n        self._continuation_primary_v4_finalizer: Callable[[_CallModelOutcome, RequiredWorkTicket], Awaitable[RequiredSQLFinishOnce[PrimaryProviderResult | PrimaryPaidOverflowTerminal]]] | None = None\n        self._continuation_primary_v4_sql_committed = False\n        self._continuation_primary_v4_terminal = False\n        self.continuation_semantic_v4: PreparedContinuationProvider | None = None\n        self.continuation_semantic_v4_admission: V4SemanticProviderAdmission | None = None\n        self.continuation_semantic_v4_result: SemanticAdvisorProviderResult | VerifiedSemanticPaidOverflowTerminal | None = None\n        self._continuation_semantic_v4_finalizer: Callable[[SemanticProviderResponseObservation, RequiredWorkTicket], Awaitable[RequiredSQLFinishOnce[SemanticAdvisorProviderResult | VerifiedSemanticPaidOverflowTerminal]]] | None = None\n        self._continuation_semantic_v4_sql_committed = False\n        self._continuation_semantic_v4_terminal = False\n\n    def bind_continuation_title(self, prepared: PreparedTitleProvider) -> None:\n        from elspeth.web.composer.continuation_clock import ContinuationActionClock\n        from elspeth.web.sessions.continuation_title_provider import PreparedTitleProvider\n        if type(prepared) is not PreparedTitleProvider or self.continuation_title is not None or self.attempt is not None or (self.required_work.role is not RequiredWorkRole.TITLE) or (self.required_work.semantic_ordinal != 6) or (self._cost_budget is not None) or (type(prepared.clock) is not ContinuationActionClock) or (prepared.work.coordinator is not self.required_work.coordinator) or (prepared.work.transition_ordinal != self.required_work.transition_ordinal) or (prepared.running.session_operation_context is not self.context) or (prepared.composer._require_sessions_service() is not self.service):\n            raise AuditIntegrityError('Durable TITLE requires its actual first family-3 invocation and original context')\n        self.continuation_title = prepared\n        self.continuation_title_selected = prepared.selected\n\n    def bind_title_no_sdk_issuer(self, issuer: TitleNoSdkIssuer) -> None:\n        from elspeth.web.composer.continuation_title_no_sdk import TitleNoSdkIssuer\n        if type(issuer) is not TitleNoSdkIssuer or self._title_no_sdk_issuer is not None or self.continuation_title is not issuer.prepared or (self.attempt is not None) or (self.call is not None):\n            raise AuditIntegrityError('TITLE no-SDK issuer lost its original prepared custody')\n        issuer.bind_custody(self)\n        self._title_no_sdk_issuer = issuer\n\n    def note_v4_title_complete(self, result: TitleAuditSettlementRequest) -> None:\n        from elspeth.web.sessions.continuation_title_provider import TitleAuditSettlementRequest\n        prepared, admission = (self.continuation_title, self.continuation_title_v4_admission)\n        if type(result) is not TitleAuditSettlementRequest or prepared is None or admission is None or (result.running is not prepared.running) or (self.attempt is not admission.attempt) or (not self._sdk_entered) or (self.call is None) or (self.call.status is not ComposerLLMCallStatus.SUCCESS) or (result.audit_call != self.call) or (result.selected == admission.selected) or (self.continuation_title_v4_terminal is not None) or (self.continuation_title_result is not None):\n            raise AuditIntegrityError('V4 TITLE complete result changed its original paid admission or settled root')\n        self.continuation_title_result = replace(result, audit_call=self.call)\n        self.continuation_title_selected = result.selected\n        self.attempt = None\n        self.call = None\n        self._sdk_entered = False\n\n    def note_v4_title_terminal(self, result: VerifiedTitlePaidOverflowTerminal) -> None:\n        from elspeth.web.sessions.continuation_title_paid_overflow_terminal import VerifiedTitlePaidOverflowTerminal\n        admission = self.continuation_title_v4_admission\n        if type(result) is not VerifiedTitlePaidOverflowTerminal or admission is None or self.attempt is not admission.attempt or (not self._sdk_entered) or (self.call is None) or (self.call.status is not ComposerLLMCallStatus.SUCCESS) or (result.source != admission.hold) or (result.provider_attempt_id != admission.attempt.attempt_id) or (result.receipt.identity.attempt_id != admission.attempt.attempt_id) or (result.receipt.identity.effect_key != admission.hold.effect_key) or (result.hold_state != 'consumed') or (self.continuation_title_result is not None) or (self.continuation_title_v4_terminal is not None):\n            raise AuditIntegrityError('V4 TITLE terminal result changed its original paid admission or cold hold proof')\n        self.continuation_title_v4_terminal = result\n        self.continuation_title_selected = result.terminal\n        self.attempt = None\n        self.call = None\n        self._sdk_entered = False\n\n    def bind_continuation_primary_clock(self, clock: ContinuationActionClock) -> None:\n        from elspeth.web.composer.continuation_clock import ContinuationActionClock\n        if type(clock) is not ContinuationActionClock or self.continuation_title is not None or self.continuation_primary_clock is not None or (self.attempt is not None) or (self.required_work.role is not RequiredWorkRole.TURN):\n            raise AuditIntegrityError('PRIMARY timing requires its original pre-dispatch action owner')\n        self.continuation_primary_clock = clock\n\n    def bind_primary_no_sdk_issuer(self, issuer: PrimaryNoSdkIssuer) -> None:\n        from elspeth.web.composer.continuation_primary_no_sdk import PrimaryNoSdkIssuer\n        if type(issuer) is not PrimaryNoSdkIssuer or self._primary_no_sdk_issuer is not None or self.continuation_primary_clock is not issuer.clock or (self.attempt is not None):\n            raise AuditIntegrityError('PRIMARY no-SDK issuer requires original unentered physical custody')\n        issuer.bind_custody(self)\n        self._primary_no_sdk_issuer = issuer\n\n    def bind_continuation_primary_v4(self, prepared: PreparedContinuationProvider, finalizer: Callable[[_CallModelOutcome, RequiredWorkTicket], Awaitable[RequiredSQLFinishOnce[PrimaryProviderResult | PrimaryPaidOverflowTerminal]]]) -> None:\n        from elspeth.web.sessions.continuation import PreparedContinuationProvider\n        from elspeth.web.sessions.service import SessionServiceImpl\n        if type(prepared) is not PreparedContinuationProvider or type(self.service) is not SessionServiceImpl or self.family is not ProviderInvocationFamily.PRIMARY or (self.required_work.role is not RequiredWorkRole.TURN) or (self._primary_no_sdk_issuer is None) or (self.continuation_primary_clock is not self._primary_no_sdk_issuer.clock) or (prepared.binding.running.session_operation_context is not self.context) or (self.continuation_primary_v4 is not None) or (self.attempt is not None) or (not callable(finalizer)):\n            raise AuditIntegrityError('V4 PRIMARY requires its original prepared, timed and unentered custody')\n        self.continuation_primary_v4 = prepared\n        self._continuation_primary_v4_finalizer = finalizer\n\n    def bind_classifier_no_sdk_issuer(self, issuer: ClassifierNoSdkIssuer) -> None:\n        from elspeth.web.composer.continuation_classifier_no_sdk import ClassifierNoSdkIssuer\n        if type(issuer) is not ClassifierNoSdkIssuer or self.family is not ProviderInvocationFamily.PRIMARY or self.continuation_primary_clock is not issuer.clock or (self._classifier_no_sdk_issuer is not None) or (self._primary_no_sdk_issuer is not None) or (self._semantic_no_sdk_issuer is not None) or (self.continuation_title is not None) or (self.attempt is not None):\n            raise AuditIntegrityError('Classifier no-SDK issuer requires its original timed PRIMARY custody')\n        issuer.bind_custody(self)\n        self._classifier_no_sdk_issuer = issuer\n\n    def bind_continuation_checkpoint_v4(self, prepared: PreparedCheckpointProvider, issuer: CheckpointNoSdkIssuer) -> None:\n        from elspeth.web.composer.continuation_checkpoint_no_sdk import CheckpointNoSdkIssuer\n        from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider\n        from elspeth.web.sessions.service import SessionServiceImpl\n        if type(prepared) is not PreparedCheckpointProvider or type(issuer) is not CheckpointNoSdkIssuer or issuer.prepared is not prepared or (issuer.clock is not prepared.clock) or (type(self.service) is not SessionServiceImpl) or (self.family is not ProviderInvocationFamily.ADVISOR) or (self.required_work.role is not RequiredWorkRole.TURN) or (prepared.work is not self.required_work) or (prepared.running.session_operation_context is not self.context) or (self.continuation_checkpoint_v4 is not None) or (self.continuation_semantic_v4 is not None) or (self.continuation_primary_v4 is not None) or (self.continuation_classifier_v4 is not None) or (self.continuation_title is not None) or (self.attempt is not None):\n            raise AuditIntegrityError('V4 checkpoint custody requires its original prepared ADVISOR invocation')\n        issuer.require_original_clock()\n        issuer.bind_custody(self)\n        self.continuation_checkpoint_v4 = prepared\n        self._checkpoint_no_sdk_issuer = issuer\n\n    def bind_continuation_classifier_v4(self, prepared: PreparedClassifierPrimaryProvider, finalizer: Callable[[_CallModelOutcome, RequiredWorkTicket], Awaitable[RequiredSQLFinishOnce[ClassifierPrimaryResult | VerifiedClassifierPaidOverflowTerminal]]]) -> None:\n        from elspeth.web.sessions.continuation_classifier_primary import PreparedClassifierPrimaryProvider\n        from elspeth.web.sessions.service import SessionServiceImpl\n        if type(prepared) is not PreparedClassifierPrimaryProvider or type(self.service) is not SessionServiceImpl or self.family is not ProviderInvocationFamily.PRIMARY or (self.required_work.role is not RequiredWorkRole.TURN) or (self._classifier_no_sdk_issuer is None) or (prepared.prepared.binding.running.session_operation_context is not self.context) or (self.continuation_primary_v4 is not None) or (self.continuation_classifier_v4 is not None) or (self.attempt is not None) or (not callable(finalizer)):\n            raise AuditIntegrityError('V4 classifier requires its original prepared, timed and unentered custody')\n        self.continuation_classifier_v4 = prepared\n        self._continuation_classifier_v4_finalizer = finalizer\n\n    @property\n    def family(self) -> ProviderInvocationFamily | None:\n        return self._family\n\n    @property\n    def continuation_checkpoint_clock(self) -> ContinuationActionClock | None:\n        issuer = self._checkpoint_no_sdk_issuer\n        if issuer is None:\n            return None\n        if self.family is not ProviderInvocationFamily.ADVISOR or self.continuation_checkpoint_v4 is not issuer.prepared:\n            raise AuditIntegrityError('Checkpoint timing requires its original prepared ADVISOR custody')\n        issuer.require_original_clock()\n        return issuer.clock\n\n    @property\n    def continuation_semantic_clock(self) -> ContinuationActionClock | None:\n        issuer = self._semantic_no_sdk_issuer\n        if issuer is None:\n            return None\n        if self.family is not ProviderInvocationFamily.ADVISOR:\n            raise AuditIntegrityError('Semantic timing requires its nominal ADVISOR mint family')\n        issuer.require_original_clock()\n        return issuer.clock\n\n    def bind_semantic_no_sdk_issuer(self, issuer: SemanticAdvisorNoSdkIssuer) -> None:\n        from elspeth.web.composer.continuation_semantic_no_sdk import SemanticAdvisorNoSdkIssuer\n        if type(issuer) is not SemanticAdvisorNoSdkIssuer or self.family is not ProviderInvocationFamily.ADVISOR or self._semantic_no_sdk_issuer is not None or (self._primary_no_sdk_issuer is not None) or (self.continuation_primary_clock is not None) or (self.continuation_title is not None) or (self.continuation_semantic_clock is not None) or (self.attempt is not None):\n            raise AuditIntegrityError('Semantic timing and no-SDK authority require original ADVISOR custody')\n        issuer.bind_custody(self)\n        self._semantic_no_sdk_issuer = issuer\n\n    def bind_continuation_semantic_v4(self, prepared: PreparedContinuationProvider, finalizer: Callable[[SemanticProviderResponseObservation, RequiredWorkTicket], Awaitable[RequiredSQLFinishOnce[SemanticAdvisorProviderResult | VerifiedSemanticPaidOverflowTerminal]]]) -> None:\n        from elspeth.web.sessions.continuation import PreparedContinuationProvider\n        from elspeth.web.sessions.service import SessionServiceImpl\n        if type(prepared) is not PreparedContinuationProvider or type(self.service) is not SessionServiceImpl or self.family is not ProviderInvocationFamily.ADVISOR or (self.required_work.role is not RequiredWorkRole.TURN) or (self._semantic_no_sdk_issuer is None) or (prepared.binding.running.session_operation_context is not self.context) or (self.continuation_primary_v4 is not None) or (self.continuation_semantic_v4 is not None) or (self.continuation_title is not None) or (self.attempt is not None) or (not callable(finalizer)):\n            raise AuditIntegrityError('V4 semantic custody requires its original prepared and unentered ADVISOR source')\n        self.continuation_semantic_v4 = prepared\n        self._continuation_semantic_v4_finalizer = finalizer\n\n    async def retain_continuation_title(self, completion: _AdmittedLLMCompletion) -> None:\n        from elspeth.web.composer.continuation_title_provider import retain_current_title_success\n        if self.continuation_title is None or self.call is None or (not self._sdk_entered):\n            raise AuditIntegrityError('TITLE retention requires its actual physical success and original audit')\n        _, cancellations = await self._join(retain_current_title_success(self, completion))\n        self.raise_deferred_cancellations(cancellations)\n\n    def prepare_cost_request(self, kwargs: CommonProviderCostRequest) -> None:\n        if self._cost_budget is not None:\n            self._cost_budget.prepare_request(kwargs)\n\n    def cost_completion_limit(self) -> int | None:\n        if self._cost_budget is None:\n            return None\n        policy = self._cost_budget.snapshot().policy\n        return policy.max_completion_tokens if policy is not None else None\n\n    @property\n    def has_cost_budget(self) -> bool:\n        return self._cost_budget is not None\n\n    def require_cost_dispatch(self) -> None:\n        if self._cost_budget is not None:\n            self._cost_budget.require_dispatch()\n\n    def require_cost_output(self) -> None:\n        if self._v4_returned_incomplete_committed or self.v4_sdk_terminal_result is not None or self._continuation_primary_v4_terminal or self._continuation_classifier_v4_terminal or self._continuation_semantic_v4_terminal:\n            return\n        if self.attempt is not None and self._sdk_entered and (self.continuation_primary_v4 is not None or self.continuation_classifier_v4_admission is not None or self.continuation_checkpoint_v4_admission is not None or (self.continuation_title_v4_admission is not None) or (self.continuation_semantic_v4_admission is not None)):\n            return\n        if self._cost_budget is not None:\n            self._cost_budget.require_output()\n\n    def mark_cost_response_accountable(self, *, admitted_metadata: bool) -> None:\n        if type(admitted_metadata) is not bool or self.attempt is None or (not self._sdk_entered):\n            raise AuditIntegrityError('response accountability requires actual dispatched custody')\n        self._cost_response_accountable = admitted_metadata\n\n    @property\n    def service(self) -> SessionServiceProtocol:\n        return self._service\n\n    @property\n    def context(self) -> SessionOperationContext:\n        return self._context\n\n    @property\n    def required_work(self) -> RequiredWorkBinding:\n        return self._required_work\n\n    async def _join[R](self, operation: Awaitable[R]) -> tuple[R, tuple[asyncio.CancelledError, ...]]:\n        task = asyncio.ensure_future(operation)\n        cancellations: list[asyncio.CancelledError] = []\n        while not task.done():\n            try:\n                await asyncio.shield(task)\n            except asyncio.CancelledError as cancellation:\n                if all((cancellation is not original for original in cancellations)):\n                    cancellations.append(cancellation)\n            except BaseException:\n                break\n        if task.cancelled():\n            try:\n                task.result()\n            except asyncio.CancelledError as child_cancellation:\n                integrity = AuditIntegrityError('Required provider child lacks an actual delivered outcome')\n                integrity.__cause__ = child_cancellation\n                if cancellations:\n                    raise BaseExceptionGroup('Provider self-cancelled child and original cancellations', [integrity, child_cancellation, *cancellations]) from None\n                raise integrity from child_cancellation\n        try:\n            return (task.result(), tuple(cancellations))\n        except BaseException as failure:\n            retained = failure\n            if isinstance(failure, Exception) and (not isinstance(failure, (*contract_errors.TIER_1_ERRORS, SQLAlchemyError, SessionOperationFenceLost, ChargeableAdmissionRefused, BaseExceptionGroup))):\n                retained = ComposerOwnedSettlementFailure()\n                retained.__cause__ = failure\n            if cancellations:\n                raise BaseExceptionGroup('Provider required child and cancellation outcomes', [retained, *cancellations]) from None\n            if retained is not failure:\n                raise retained from failure\n            raise\n\n    async def _v4_admission_result[R](self, operation: Coroutine[Any, Any, RequiredSQLFinishOnce[R | _ProviderAdmissionRefused]], *, admission_projection: RequiredWorkTicket, readback: Callable[[RequiredWorkTicket], Coroutine[Any, Any, RequiredSQLFinishOnce[R | None]]] | None) -> _V4AdmissionProjection[R]:\n        from elspeth.web.sessions.service import _ProviderAdmissionRefused\n        physical, outer = await self._join(operation)\n        if type(physical) is RequiredSQLRaised:\n            admission_projection.complete_without_submission()\n            read_failure: BaseException | None = None\n            recovered: R | None = None\n            read_deferred: tuple[asyncio.CancelledError, ...] = ()\n            if readback is not None:\n                read_sql, read_projection = self.required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)\n                try:\n                    observed, read_outer = await self._join(readback(read_sql))\n                except BaseException as error:\n                    if read_sql.complete:\n                        read_projection.complete_without_submission()\n                    read_failure = error\n                else:\n                    if type(observed) is RequiredSQLRaised:\n                        read_projection.complete_without_submission()\n                        read_failure = observed.error\n                        read_deferred = (*observed.deferred_cancellations, *read_outer)\n                    elif type(observed) is RequiredSQLReturned:\n                        read_projection.begin_projection()\n                        read_projection.complete_owned()\n                        recovered = observed.value\n                        read_deferred = (*observed.deferred_cancellations, *read_outer)\n                    else:\n                        read_failure = AuditIntegrityError('V4 admission cold read lost its actual required SQL outcome')\n            failures: list[BaseException] = [physical.error]\n            if read_failure is not None and all((read_failure is not prior for prior in failures)):\n                failures.append(read_failure)\n            deferred: list[asyncio.CancelledError] = []\n            for cancellation in (*physical.deferred_cancellations, *outer, *read_deferred):\n                if all((cancellation is not prior for prior in failures)):\n                    failures.append(cancellation)\n                if all((cancellation is not prior for prior in deferred)):\n                    deferred.append(cancellation)\n            if recovered is not None and read_failure is None:\n                return _V4AdmissionProjection(recovered, tuple(deferred), physical.error, True)\n            if len(failures) == 1:\n                raise physical.error\n            raise BaseExceptionGroup('V4 provider admission SQL and original cancellations', failures)\n        if type(physical) is not RequiredSQLReturned:\n            raise AuditIntegrityError('V4 provider admission lost its actual required SQL outcome')\n        returned_deferred: list[asyncio.CancelledError] = []\n        for cancellation in (*physical.deferred_cancellations, *outer):\n            if all((cancellation is not prior for prior in returned_deferred)):\n                returned_deferred.append(cancellation)\n        if type(physical.value) is _ProviderAdmissionRefused:\n            if returned_deferred:\n                raise BaseExceptionGroup('V4 provider quota refusal and original cancellations', [physical.value.refusal, *returned_deferred])\n            raise physical.value.refusal\n        return _V4AdmissionProjection(cast('R', physical.value), tuple(returned_deferred), None, False)\n\n    def raise_deferred_cancellations(self, cancellations: tuple[asyncio.CancelledError, ...]) -> None:\n        if len(cancellations) == 1:\n            raise cancellations[0]\n        if cancellations:\n            raise BaseExceptionGroup('Provider original cancellations', list(cancellations))\n\n    async def join_title_update[R](self, operation: Coroutine[Any, Any, R]) -> tuple[R, tuple[asyncio.CancelledError, ...]]:\n        if self.required_work.role is not RequiredWorkRole.TITLE or self.attempt is not None:\n            raise AuditIntegrityError('Title publication cannot precede provider accounting completion')\n        return await self._join(operation)\n\n    async def _cancel_undispatched(self, *, model: str) -> tuple[asyncio.CancelledError, ...]:\n        if self.attempt is None or self._sdk_entered:\n            raise AuditIntegrityError('Undispatched disposition lacks exact no-SDK attempt evidence')\n        if self.continuation_primary_v4_admission is not None or self.continuation_classifier_v4_admission is not None or self.continuation_checkpoint_v4_admission is not None or (self.continuation_semantic_v4_admission is not None) or (self.continuation_title_v4_admission is not None):\n            raise AuditIntegrityError('V4 undispatched hold requires its composite SQL disposition')\n        admitted = self.attempt\n        binding = self.required_work\n        ticket = binding.coordinator.reserve(RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL, transition_ordinal=binding.transition_ordinal, semantic_ordinal=binding.semantic_ordinal, recurrence_ordinal=self._undispatched_recurrence)\n        self._undispatched_recurrence += 1\n        _, cancellations = await self._join(self.service.cancel_undispatched_provider_attempt(session_operation_context=self.context, attempt_id=self.attempt.attempt_id, requested_model=model, required_work=ticket))\n        if self._primary_no_sdk_issuer is not None:\n            self._primary_no_sdk_issuer.note_undispatched_cancel(self, admitted, model=model)\n        if self._semantic_no_sdk_issuer is not None:\n            self._semantic_no_sdk_issuer.note_undispatched_cancel(self, admitted, model=model)\n        self.attempt = None\n        self.call = None\n        return cancellations\n\n    async def cancel_before_sdk(self, *, model: str) -> tuple[asyncio.CancelledError, ...]:\n        \"\"\"Dispose an actually admitted attempt whose physical SDK never began.\"\"\"\n        return await self._cancel_undispatched(model=model)\n\n    async def admit(self, *, model: str) -> None:\n        if type(model) is not str or not model or self._admitting:\n            raise AuditIntegrityError('Provider admission lacks a single owned model invocation')\n        self._admitting = True\n        try:\n            await self.settle()\n            self.require_cost_dispatch()\n            self._cost_response_accountable = False\n            title = self.required_work.role is RequiredWorkRole.TITLE\n            sql, projection = self.required_work.reserve_pair(RequiredWorkSource.TITLE_PROVIDER_ADMISSION_SQL if title else RequiredWorkSource.PROVIDER_ADMISSION_SQL, RequiredWorkSource.TITLE_PROVIDER_ADMISSION_PROJECTION if title else RequiredWorkSource.PROVIDER_ADMISSION_PROJECTION)\n            cold_recovered = False\n            recovered_failure: BaseException | None = None\n            try:\n                if self.continuation_checkpoint_v4 is not None:\n                    from elspeth.web.async_workers import run_required_sql_finish_once\n                    from elspeth.web.sessions.continuation_checkpoint_paid import V4CheckpointProviderAdmission\n                    from elspeth.web.sessions.service import SessionServiceImpl\n                    checkpoint_service = self.service\n                    checkpoint_prepared = self.continuation_checkpoint_v4\n                    if type(checkpoint_service) is not SessionServiceImpl or self._checkpoint_no_sdk_issuer is None:\n                        raise AuditIntegrityError('V4 checkpoint admission requires its actual session and no-SDK owners')\n                    checkpoint_outcome, checkpoint_outer = await self._join(run_required_sql_finish_once(sql, checkpoint_service.begin_v4_checkpoint_provider_attempt_sync, prepared=checkpoint_prepared, requested_model=model))\n                    if type(checkpoint_outcome) is RequiredSQLRaised:\n                        projection.complete_without_submission()\n                        checkpoint_failures: list[BaseException] = [checkpoint_outcome.error]\n                        checkpoint_failures.extend((*checkpoint_outcome.deferred_cancellations, *checkpoint_outer))\n                        if len(checkpoint_failures) == 1:\n                            raise checkpoint_outcome.error\n                        raise BaseExceptionGroup('V4 checkpoint admission SQL and original cancellations', checkpoint_failures)\n                    if type(checkpoint_outcome) is not RequiredSQLReturned:\n                        raise AuditIntegrityError('V4 checkpoint admission lost its actual required SQL outcome')\n                    checkpoint_admitted = checkpoint_outcome.value\n                    if type(checkpoint_admitted) is not V4CheckpointProviderAdmission:\n                        raise AuditIntegrityError('V4 checkpoint admission lost its exact physical and hold result')\n                    attempt = checkpoint_admitted.attempt\n                    cancellations = (*checkpoint_outcome.deferred_cancellations, *checkpoint_outer)\n                elif self.continuation_classifier_v4 is not None:\n                    from elspeth.web.sessions.continuation_classifier_primary import V4ClassifierProviderAdmission\n                    from elspeth.web.sessions.service import SessionServiceImpl\n                    classifier_service = self.service\n                    classifier_prepared = self.continuation_classifier_v4\n                    classifier_issuer = self._classifier_no_sdk_issuer\n                    if type(classifier_service) is not SessionServiceImpl or classifier_issuer is None:\n                        raise AuditIntegrityError('V4 classifier admission requires its actual session and no-SDK owners')\n                    classifier_projected = await self._v4_admission_result(classifier_service.begin_v4_continuation_classifier_attempt(prepared=classifier_prepared, requested_model=model, no_sdk_issuer=classifier_issuer, required_work=sql), admission_projection=projection, readback=lambda read_sql: classifier_service.read_v4_continuation_classifier_admission(prepared=classifier_prepared, no_sdk_issuer=classifier_issuer, required_work=read_sql))\n                    classifier_admitted = classifier_projected.admitted\n                    cancellations = classifier_projected.deferred_cancellations\n                    cold_recovered = classifier_projected.cold_recovered\n                    recovered_failure = classifier_projected.original_failure\n                    if type(classifier_admitted) is not V4ClassifierProviderAdmission:\n                        raise AuditIntegrityError('V4 classifier admission lost its exact physical and hold result')\n                    classifier_issuer.bind_dispatched(classifier_admitted.dispatched)\n                    attempt = classifier_admitted.attempt\n                    self.continuation_classifier_v4_admission = classifier_admitted\n                elif self.continuation_primary_v4 is not None:\n                    from elspeth.web.sessions.continuation_primary import V4PrimaryProviderAdmission\n                    from elspeth.web.sessions.service import SessionServiceImpl\n                    primary_service = self.service\n                    primary_prepared = self.continuation_primary_v4\n                    primary_issuer = self._primary_no_sdk_issuer\n                    if type(primary_service) is not SessionServiceImpl or primary_issuer is None:\n                        raise AuditIntegrityError('V4 PRIMARY admission requires its actual session and no-SDK owners')\n                    primary_projected = await self._v4_admission_result(primary_service.begin_v4_continuation_primary_attempt(prepared=primary_prepared, requested_model=model, no_sdk_issuer=primary_issuer, required_work=sql), admission_projection=projection, readback=lambda read_sql: primary_service.read_v4_continuation_primary_admission(prepared=primary_prepared, required_work=read_sql))\n                    primary_admitted = primary_projected.admitted\n                    cancellations = primary_projected.deferred_cancellations\n                    cold_recovered = primary_projected.cold_recovered\n                    recovered_failure = primary_projected.original_failure\n                    if type(primary_admitted) is not V4PrimaryProviderAdmission:\n                        raise AuditIntegrityError('V4 PRIMARY admission lost its exact physical and hold result')\n                    if cold_recovered:\n                        primary_issuer.bind_cold_recovered_dispatched(primary_admitted.selected)\n                    else:\n                        primary_issuer.bind_dispatched(primary_admitted.selected)\n                    attempt = primary_admitted.attempt\n                    self.continuation_primary_v4_admission = primary_admitted\n                elif self.continuation_semantic_v4 is not None:\n                    from elspeth.web.sessions.continuation_semantic_advisor import V4SemanticProviderAdmission\n                    from elspeth.web.sessions.service import SessionServiceImpl\n                    semantic_service = self.service\n                    semantic_prepared = self.continuation_semantic_v4\n                    semantic_issuer = self._semantic_no_sdk_issuer\n                    if type(semantic_service) is not SessionServiceImpl or semantic_issuer is None:\n                        raise AuditIntegrityError('V4 semantic admission requires its actual session and no-SDK owners')\n                    semantic_projected = await self._v4_admission_result(semantic_service.begin_v4_continuation_semantic_attempt(prepared=semantic_prepared, requested_model=model, no_sdk_issuer=semantic_issuer, required_work=sql), admission_projection=projection, readback=lambda read_sql: semantic_service.read_v4_continuation_semantic_admission(prepared=semantic_prepared, no_sdk_issuer=semantic_issuer, required_work=read_sql))\n                    semantic_admitted = semantic_projected.admitted\n                    cancellations = semantic_projected.deferred_cancellations\n                    cold_recovered = semantic_projected.cold_recovered\n                    recovered_failure = semantic_projected.original_failure\n                    if type(semantic_admitted) is not V4SemanticProviderAdmission:\n                        raise AuditIntegrityError('V4 semantic admission lost its exact physical and hold result')\n                    semantic_issuer.bind_dispatched(semantic_admitted.selected)\n                    attempt = semantic_admitted.attempt\n                    self.continuation_semantic_v4_admission = semantic_admitted\n                elif self.continuation_title is None:\n                    attempt, cancellations = await self._join(self.service.begin_provider_attempt(session_operation_context=self.context, source='auto_title' if title else 'composer', required_work=sql))\n                else:\n                    from elspeth.web.sessions.continuation_title_provider import V4TitleProviderAdmission\n                    from elspeth.web.sessions.service import SessionServiceImpl\n                    title_service = self.service\n                    title_prepared = self.continuation_title\n                    if type(title_service) is not SessionServiceImpl or self._title_no_sdk_issuer is None:\n                        raise AuditIntegrityError('TITLE admission requires its actual sessions and no-SDK owners')\n                    if title_prepared is None:\n                        raise AuditIntegrityError('TITLE admission lost its original prepared producer')\n                    title_projected = await self._v4_admission_result(title_service.begin_v4_continuation_title_attempt(prepared=title_prepared, requested_model=model, required_work=sql), admission_projection=projection, readback=lambda read_sql: title_service.read_v4_continuation_title_admission(prepared=title_prepared, required_work=read_sql))\n                    title_admitted = title_projected.admitted\n                    cancellations = title_projected.deferred_cancellations\n                    cold_recovered = title_projected.cold_recovered\n                    recovered_failure = title_projected.original_failure\n                    if type(title_admitted) is not V4TitleProviderAdmission:\n                        raise AuditIntegrityError('V4 TITLE admission lost its original held physical result')\n                    attempt = title_admitted.attempt\n                    self.continuation_title_selected = title_admitted.selected\n                    self.continuation_title_v4_admission = title_admitted\n            except BaseException:\n                if sql.complete and (not projection.complete):\n                    projection.complete_without_submission()\n                raise\n            if not cold_recovered:\n                projection.begin_projection()\n            try:\n                if type(attempt) is not ProviderAttempt:\n                    raise AuditIntegrityError('Provider admission returned a foreign attempt')\n                self.attempt = attempt\n                if self._title_no_sdk_issuer is not None:\n                    title_v4_admission = self.continuation_title_v4_admission\n                    if title_v4_admission is None:\n                        raise AuditIntegrityError('TITLE no-SDK issuer requires the original V4 physical hold')\n                    self._title_no_sdk_issuer.bind_admission(self, title_v4_admission, observed_model=model)\n                if self.continuation_checkpoint_v4 is not None:\n                    self.continuation_checkpoint_v4_admission = checkpoint_admitted\n                    if self._checkpoint_no_sdk_issuer is None:\n                        raise AuditIntegrityError('V4 checkpoint admission lost its original no-SDK owner')\n                    self._checkpoint_no_sdk_issuer.bind_admission(self, checkpoint_admitted, observed_model=model)\n                if self._primary_no_sdk_issuer is not None:\n                    self._primary_no_sdk_issuer.note_admitted_attempt(self, attempt, model=model)\n                if self._classifier_no_sdk_issuer is not None:\n                    self._classifier_no_sdk_issuer.note_admitted_attempt(self, attempt, model=model)\n                if self._semantic_no_sdk_issuer is not None:\n                    self._semantic_no_sdk_issuer.note_admitted_attempt(self, attempt, model=model)\n                self._admitted_ns = time.monotonic_ns()\n                self.call = None\n                self._sdk_entered = False\n            except BaseException as failure:\n                if not cold_recovered:\n                    projection.complete_owned(failure)\n                raise\n            if not cold_recovered:\n                projection.complete_owned()\n            if recovered_failure is not None:\n                failures: list[BaseException] = [recovered_failure]\n                for cancellation in cancellations:\n                    if all((cancellation is not prior for prior in failures)):\n                        failures.append(cancellation)\n                if len(failures) == 1:\n                    raise recovered_failure\n                raise BaseExceptionGroup('V4 recovered admission and original cancellations', failures)\n            if cancellations:\n                if self.has_v4_unentered_hold():\n                    self.raise_deferred_cancellations(cancellations)\n                additional = await self._cancel_undispatched(model=model)\n                self.raise_deferred_cancellations((*cancellations, *additional))\n        finally:\n            self._admitting = False\n\n    def mark_sdk_entered(self) -> None:\n        if self.attempt is None or self._sdk_entered or self.call is not None:\n            raise AuditIntegrityError('Physical provider dispatch lacks exact admitted custody')\n        if self._primary_no_sdk_issuer is not None:\n            self._primary_no_sdk_issuer.mark_sdk_entered(self)\n        if self._classifier_no_sdk_issuer is not None:\n            self._classifier_no_sdk_issuer.note_sdk_entry(self)\n        if self._semantic_no_sdk_issuer is not None:\n            self._semantic_no_sdk_issuer.mark_sdk_entered(self)\n        if self._title_no_sdk_issuer is not None:\n            self._title_no_sdk_issuer.note_sdk_entry(self)\n        if self._cost_budget is not None:\n            self._cost_budget.dispatched()\n        self._sdk_entered = True\n\n    def has_undispatched_attempt(self) -> bool:\n        return self.attempt is not None and (not self._sdk_entered)\n\n    def has_v4_unentered_hold(self) -> bool:\n        attempt = self.attempt\n        return attempt is not None and (not self._sdk_entered) and (self.continuation_primary_v4_admission is not None and attempt is self.continuation_primary_v4_admission.attempt or (self.continuation_classifier_v4_admission is not None and attempt is self.continuation_classifier_v4_admission.attempt) or (self.continuation_checkpoint_v4_admission is not None and attempt is self.continuation_checkpoint_v4_admission.attempt) or (self.continuation_semantic_v4_admission is not None and attempt is self.continuation_semantic_v4_admission.attempt) or (self.continuation_title_v4_admission is not None and attempt is self.continuation_title_v4_admission.attempt))\n\n    def requires_v4_original_result(self) -> bool:\n        \"\"\"Select original SDK-return admission from this exact entered V4 attempt.\"\"\"\n        admissions = tuple((admission for admission in (self.continuation_checkpoint_v4_admission, self.continuation_classifier_v4_admission, self.continuation_primary_v4_admission, self.continuation_semantic_v4_admission, self.continuation_title_v4_admission) if admission is not None))\n        if not admissions:\n            if any((prepared is not None for prepared in (self.continuation_checkpoint_v4, self.continuation_classifier_v4, self.continuation_primary_v4, self.continuation_semantic_v4))):\n                raise AuditIntegrityError('V4 provider SDK return lacks its original admitted physical attempt')\n            return False\n        if len(admissions) != 1 or self.attempt is not admissions[0].attempt or (not self._sdk_entered):\n            raise AuditIntegrityError('V4 provider SDK return changed its original paid attempt')\n        return True\n\n    def bind_v4_sdk_finalizer(self, callback: Callable[[V4SdkFailureObservation, RequiredWorkTicket], Awaitable[RequiredSQLFinishOnce[VerifiedSdkTerminal]]]) -> None:\n        if self._v4_sdk_finalizer is not None or not callable(callback) or self.attempt is not None:\n            raise AuditIntegrityError('SDK finalizer requires one original pre-admission owner')\n        self._v4_sdk_finalizer = callback\n\n    def has_v4_sdk_finalizer(self) -> bool:\n        return self._v4_sdk_finalizer is not None\n\n    async def finalize_v4_sdk_failure(self, observation: V4SdkFailureObservation, *, preserve_sdk_cancellation: bool) -> V4SdkFinalization:\n        from elspeth.web.composer.provider_sdk_observation import forget_v4_sdk_failure, require_original_v4_sdk_failure\n        from elspeth.web.sessions.continuation_sdk_terminal_result import VerifiedSdkTerminal\n        require_original_v4_sdk_failure(observation, custody=self)\n        finalizer = self._v4_sdk_finalizer\n        admissions = tuple((item for item in (self.continuation_primary_v4_admission, self.continuation_semantic_v4_admission, self.continuation_classifier_v4_admission, self.continuation_title_v4_admission, self.continuation_checkpoint_v4_admission) if item is not None))\n        if finalizer is None or len(admissions) != 1 or self.attempt is not admissions[0].attempt or (self.v4_sdk_terminal_result is not None):\n            raise AuditIntegrityError('SDK terminal lost its sole original finalizer')\n        admission = admissions[0]\n        sql, projection = self.required_work.reserve_pair(RequiredWorkSource.PROVIDER_SETTLEMENT_SQL, RequiredWorkSource.PROVIDER_SETTLEMENT_PROJECTION)\n        try:\n            outcome, cancellations = await self._join(finalizer(observation, sql))\n        except BaseException:\n            if sql.complete:\n                projection.complete_without_submission()\n            raise\n        if type(outcome) is RequiredSQLRaised:\n            if sql.complete:\n                projection.complete_without_submission()\n            failures: list[BaseException] = [outcome.error]\n            for cancellation in (*outcome.deferred_cancellations, *cancellations):\n                if all((cancellation is not prior for prior in failures)):\n                    failures.append(cancellation)\n            if len(failures) == 1:\n                raise outcome.error\n            raise BaseExceptionGroup('SDK terminal SQL and original cancellations', failures)\n        if type(outcome) is not RequiredSQLReturned or not sql.complete:\n            raise AuditIntegrityError('SDK terminal lost its completed required SQL outcome')\n        verified = outcome.value\n        if type(verified) is not VerifiedSdkTerminal or verified.source != admission.hold or verified.provider_attempt_id != admission.attempt.attempt_id or (verified.hold_state != 'consumed'):\n            raise AuditIntegrityError('SDK terminal changed its cold-proved original hold')\n        deferred: list[asyncio.CancelledError] = []\n        original_cancel = (observation.original,) if preserve_sdk_cancellation and isinstance(observation.original, asyncio.CancelledError) else ()\n        for cancellation in (*original_cancel, *outcome.deferred_cancellations, *cancellations):\n            if all((cancellation is not prior for prior in deferred)):\n                deferred.append(cancellation)\n        result = V4SdkFinalization(verified, tuple(deferred))\n        self.v4_sdk_terminal_result = result\n        forget_v4_sdk_failure(observation, custody=self)\n        self.attempt = None\n        self.call = None\n        self._sdk_entered = False\n        projection.begin_projection()\n        projection.complete_owned()\n        return result\n\n    def has_v4_sdk_return(self) -> bool:\n        return self._v4_sdk_return is not None\n\n    def note_v4_sdk_returned(self, response: object) -> None:\n        from elspeth.web.composer.provider_result_admission import V4ProviderSdkReturn\n        if not self.requires_v4_original_result():\n            return\n        if self.attempt is None or self._v4_sdk_return is not None:\n            raise AuditIntegrityError('V4 provider returned more than one result for its original paid attempt')\n        self._v4_sdk_return = V4ProviderSdkReturn(self.attempt.attempt_id, id(response))\n\n    def claim_v4_sdk_returned(self, response: object) -> V4ProviderSdkReturn | None:\n        if not self.requires_v4_original_result():\n            return None\n        witness = self._v4_sdk_return\n        if witness is None or self.attempt is None or witness.attempt_id != self.attempt.attempt_id or (witness.response_identity != id(response)) or self._v4_sdk_return_claimed:\n            raise AuditIntegrityError('V4 provider parser lost its actual one-time SDK return')\n        self._v4_sdk_return_claimed = True\n        return witness\n\n    def require_v4_result_completeness(self, witness: V4ProviderResultCompleteness) -> None:\n        from elspeth.web.composer.provider_result_admission import V4ProviderResultCompleteness\n        if type(witness) is not V4ProviderResultCompleteness or not self.requires_v4_original_result() or (not self._v4_sdk_return_claimed) or (witness.sdk_return is not self._v4_sdk_return):\n            raise AuditIntegrityError('V4 result completeness lost its original one-time SDK return')\n\n    def require_v4_result_incomplete(self, incomplete: V4ProviderResultIncomplete) -> None:\n        from elspeth.web.composer.provider_result_admission import V4ProviderResultIncomplete, require_issued_v4_incomplete\n        if type(incomplete) is not V4ProviderResultIncomplete or not self.requires_v4_original_result() or (not self._v4_sdk_return_claimed) or (incomplete.sdk_return is not self._v4_sdk_return):\n            raise AuditIntegrityError('V4 incomplete result lost its original one-time SDK return')\n        require_issued_v4_incomplete(incomplete, sdk_return=self._v4_sdk_return)\n\n    def bind_v4_returned_incomplete_finalizer(self, callback: Callable[[V4ProviderResultIncomplete, RequiredWorkTicket], Awaitable[RequiredSQLFinishOnce[VerifiedPaidIncompleteTerminal]]]) -> None:\n        if self._v4_returned_incomplete_finalizer is not None or not callable(callback):\n            raise AuditIntegrityError('V4 returned-incomplete finalizer must have one original owner')\n        self._v4_returned_incomplete_finalizer = callback\n\n    def has_v4_returned_incomplete_finalizer(self) -> bool:\n        return self._v4_returned_incomplete_finalizer is not None\n\n    def has_v4_returned_incomplete_terminal(self) -> bool:\n        return self._v4_returned_incomplete_committed\n\n    def require_v4_paid_incomplete_committed(self, control: V4PaidIncompleteCommitted) -> V4ReturnedIncompleteFinalization:\n        if type(control) is not V4PaidIncompleteCommitted or not self._v4_returned_incomplete_committed or self.v4_returned_incomplete_result is not control.finalization:\n            raise AuditIntegrityError(\"Paid incomplete control lacks this custody's committed terminal\")\n        return control.finalization\n\n    async def finalize_v4_returned_incomplete(self, incomplete: V4ProviderResultIncomplete) -> V4ReturnedIncompleteFinalization:\n        from elspeth.web.sessions.continuation_paid_incomplete_terminal import VerifiedPaidIncompleteTerminal\n        self.require_v4_result_incomplete(incomplete)\n        admission = tuple((item for item in (self.continuation_checkpoint_v4_admission, self.continuation_classifier_v4_admission, self.continuation_primary_v4_admission, self.continuation_semantic_v4_admission, self.continuation_title_v4_admission) if item is not None))\n        finalizer = self._v4_returned_incomplete_finalizer\n        if len(admission) != 1 or self.attempt is not admission[0].attempt or self.call is None or (self.call.call_id != self.attempt.attempt_id) or (self.call.status is not ComposerLLMCallStatus.MALFORMED_RESPONSE) or (self.v4_returned_incomplete_result is not None) or (finalizer is None):\n            raise AuditIntegrityError('V4 returned-incomplete finalization lost its original paid audit and hold')\n        sql, projection = self.required_work.reserve_pair(RequiredWorkSource.PROVIDER_SETTLEMENT_SQL, RequiredWorkSource.PROVIDER_SETTLEMENT_PROJECTION)\n        try:\n            outcome, cancellations = await self._join(finalizer(incomplete, sql))\n        except BaseException:\n            if sql.complete:\n                projection.complete_without_submission()\n            raise\n        if type(outcome) is RequiredSQLRaised:\n            if sql.complete:\n                projection.complete_without_submission()\n            failures: list[BaseException] = [outcome.error]\n            for cancellation in (*outcome.deferred_cancellations, *cancellations):\n                if all((cancellation is not prior for prior in failures)):\n                    failures.append(cancellation)\n            if len(failures) == 1:\n                raise outcome.error\n            raise BaseExceptionGroup('V4 returned-incomplete SQL and original cancellations', failures)\n        if type(outcome) is not RequiredSQLReturned:\n            raise AuditIntegrityError('V4 returned-incomplete finalizer lost its required SQL outcome')\n        if not sql.complete:\n            raise AuditIntegrityError('V4 returned-incomplete SQL returned without its completed work ticket')\n        self._v4_returned_incomplete_committed = True\n        verified = outcome.value\n        if type(verified) is not VerifiedPaidIncompleteTerminal or verified.source != admission[0].hold or verified.provider_attempt_id != self.attempt.attempt_id or (verified.hold_state != 'consumed'):\n            raise AuditIntegrityError('V4 returned-incomplete terminal changed its original cold-proved hold')\n        projection.begin_projection()\n        projection.complete_owned()\n        deferred: list[asyncio.CancelledError] = []\n        for cancellation in (*outcome.deferred_cancellations, *cancellations):\n            if all((cancellation is not prior for prior in deferred)):\n                deferred.append(cancellation)\n        result = V4ReturnedIncompleteFinalization(verified, tuple(deferred))\n        self.v4_returned_incomplete_result = result\n        self.attempt = None\n        self.call = None\n        self._sdk_entered = False\n        return result\n\n    def require_v4_primary_unentered(self, admission: V4PrimaryProviderAdmission) -> None:\n        from elspeth.web.sessions.continuation_primary import V4PrimaryProviderAdmission\n        if type(admission) is not V4PrimaryProviderAdmission or self.continuation_primary_v4_admission is not admission or self.attempt is not admission.attempt or self._sdk_entered:\n            raise AuditIntegrityError('V4 PRIMARY no-SDK disposition lost its exact unentered physical hold')\n\n    def require_v4_classifier_unentered(self, admission: V4ClassifierProviderAdmission) -> None:\n        from elspeth.web.sessions.continuation_classifier_primary import V4ClassifierProviderAdmission\n        if type(admission) is not V4ClassifierProviderAdmission or self.continuation_classifier_v4_admission is not admission or self.attempt is not admission.attempt or self._sdk_entered:\n            raise AuditIntegrityError('V4 classifier no-SDK disposition lost its exact unentered physical hold')\n\n    def require_v4_title_unentered(self, admission: V4TitleProviderAdmission) -> None:\n        from elspeth.web.sessions.continuation_title_provider import V4TitleProviderAdmission\n        prepared = self.continuation_title\n        if type(admission) is not V4TitleProviderAdmission or prepared is None or self.continuation_title_v4_admission is not admission or (self.continuation_title_selected is not admission.selected) or (self.attempt is not admission.attempt) or self._sdk_entered or (self._v4_sdk_return is not None) or (self.call is not None) or (prepared.running.session_operation_context is not self.context):\n            raise AuditIntegrityError('V4 TITLE no-SDK disposition lost its exact unentered physical hold')\n\n    def require_v4_checkpoint_unentered(self, admission: V4CheckpointProviderAdmission) -> None:\n        from elspeth.web.sessions.continuation_checkpoint_paid import V4CheckpointProviderAdmission\n        if type(admission) is not V4CheckpointProviderAdmission or self.continuation_checkpoint_v4_admission is not admission or self.attempt is not admission.attempt or self._sdk_entered:\n            raise AuditIntegrityError('V4 checkpoint no-SDK disposition lost its exact unentered physical hold')\n\n    def note_v4_primary_no_sdk(self, receipt: PrimaryNoSdkRefusalReceipt, admission: V4PrimaryProviderAdmission) -> None:\n        from elspeth.web.sessions.continuation_primary import PrimaryNoSdkRefusalReceipt\n        self.require_v4_primary_unentered(admission)\n        if type(receipt) is not PrimaryNoSdkRefusalReceipt or receipt.effect_key != admission.hold.effect_key or receipt.terminal.status != 'failed' or ((receipt.terminal.checkpoint_id, receipt.terminal.journal_cursor) != (receipt.refused.checkpoint_id, receipt.refused.cursor)) or (self.continuation_primary_v4_no_sdk_result is not None) or (self.continuation_primary_v4_result is not None):\n            raise AuditIntegrityError('V4 PRIMARY no-SDK receipt lost its exact failed terminal')\n        self.continuation_primary_v4_no_sdk_result = receipt\n        self.attempt = None\n        self.call = None\n\n    def note_v4_classifier_no_sdk(self, receipt: ClassifierNoSdkRefusalReceipt, admission: V4ClassifierProviderAdmission) -> None:\n        from elspeth.web.sessions.continuation_classifier_primary_no_sdk import ClassifierNoSdkRefusalReceipt\n        self.require_v4_classifier_unentered(admission)\n        if type(receipt) is not ClassifierNoSdkRefusalReceipt or receipt.effect_key != admission.effect_key or receipt.terminal.status != 'failed' or ((receipt.terminal.checkpoint_id, receipt.terminal.journal_cursor) != (receipt.refused.checkpoint_id, receipt.refused.cursor)) or (self.continuation_classifier_v4_no_sdk_result is not None) or (self.continuation_classifier_v4_result is not None):\n            raise AuditIntegrityError('V4 classifier no-SDK receipt lost its exact failed terminal')\n        self.continuation_classifier_v4_no_sdk_result = receipt\n        self.attempt = None\n        self.call = None\n\n    def needs_terminal_audit(self) -> bool:\n        return self.attempt is not None and self._sdk_entered and (self.call is None)\n\n    def bind_call(self, call: ComposerLLMCall) -> ComposerLLMCall:\n        if type(call) is not ComposerLLMCall:\n            raise AuditIntegrityError('Provider audit is not an owned complete call')\n        if self.attempt is None or not self._sdk_entered:\n            raise AuditIntegrityError('Provider audit lacks actual dispatched admission')\n        bound = _bind_attempt_call(call, self.attempt, self._admitted_ns)\n        self.call = bound\n        return bound\n\n    def retain_audit(self, call: ComposerLLMCall) -> bool:\n        if type(call) is not ComposerLLMCall:\n            raise AuditIntegrityError('Provider retained audit is not nominal')\n        if self.attempt is None:\n            return False\n        if not self._sdk_entered or call.call_id != self.attempt.attempt_id or call.started_at != self.attempt.started_at:\n            raise AuditIntegrityError('Provider audit replaced actual admitted identity')\n        if self._cost_budget is not None and self._cost_budget.snapshot().applicability == 'continuation_v2' and (call.cost_response_accountable is None or call.cost_response_accountable is not self._cost_response_accountable):\n            raise AuditIntegrityError('Continuation audit changed its actual response accountability')\n        self.call = call\n        return True\n\n    async def finalize_continuation_primary_v4(self, outcome: _CallModelOutcome) -> None:\n        from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome\n        from elspeth.web.sessions.continuation_primary import PrimaryPaidOverflowTerminal, PrimaryProviderResult\n        finalizer = self._continuation_primary_v4_finalizer\n        if type(outcome) is not _CallModelOutcome or finalizer is None or self.continuation_primary_v4 is None or (self.continuation_primary_v4_admission is None) or (self.continuation_primary_v4_result is not None) or (self.attempt is None) or (not self._sdk_entered) or (self.call is None) or (self.call.status is not ComposerLLMCallStatus.SUCCESS) or (outcome.audit_call is not self.call):\n            raise AuditIntegrityError('V4 PRIMARY finalization requires its one actual paid success and original audit')\n        binding = self.required_work\n        sql, projection = binding.reserve_pair(RequiredWorkSource.PROVIDER_SETTLEMENT_SQL, RequiredWorkSource.PROVIDER_SETTLEMENT_PROJECTION)\n        try:\n            settled, cancellations = await self._join(finalizer(outcome, sql))\n        except BaseException:\n            if sql.complete:\n                projection.complete_without_submission()\n            raise\n        if type(settled) is RequiredSQLRaised:\n            if sql.complete:\n                projection.complete_without_submission()\n            failures: list[BaseException] = [settled.error]\n            for cancellation in (*settled.deferred_cancellations, *cancellations):\n                if all((cancellation is not prior for prior in failures)):\n                    failures.append(cancellation)\n            if len(failures) == 1:\n                raise settled.error\n            raise BaseExceptionGroup('PRIMARY V4 required SQL and original cancellations', failures)\n        if type(settled) is not RequiredSQLReturned:\n            raise AuditIntegrityError('V4 PRIMARY finalizer lost its actual required SQL outcome')\n        result = settled.value\n        self._continuation_primary_v4_sql_committed = True\n        if type(result) not in (PrimaryProviderResult, PrimaryPaidOverflowTerminal):\n            raise AuditIntegrityError('V4 PRIMARY finalizer returned a foreign paid outcome')\n        self._continuation_primary_v4_terminal = type(result) is PrimaryPaidOverflowTerminal\n        projection.begin_projection()\n        projection.complete_owned()\n        if self._cost_budget is not None:\n            self._cost_budget.consume_settled(self.call, response_accountable=self._cost_response_accountable)\n        self.continuation_primary_v4_result = result\n        self.attempt = None\n        self.call = None\n        self._sdk_entered = False\n        deferred: list[asyncio.CancelledError] = []\n        for cancellation in (*settled.deferred_cancellations, *cancellations):\n            if all((cancellation is not prior for prior in deferred)):\n                deferred.append(cancellation)\n        self.continuation_primary_v4_deferred_cancellations = tuple(deferred)\n\n    async def finalize_continuation_classifier_v4(self, outcome: _CallModelOutcome) -> None:\n        from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome\n        from elspeth.web.sessions.continuation_classifier_primary import ClassifierPrimaryResult, VerifiedClassifierPaidOverflowTerminal\n        finalizer = self._continuation_classifier_v4_finalizer\n        if type(outcome) is not _CallModelOutcome or finalizer is None or self.continuation_classifier_v4 is None or (self.continuation_classifier_v4_admission is None) or (self.continuation_classifier_v4_result is not None) or (self.attempt is None) or (not self._sdk_entered) or (self.call is None) or (self.call.status is not ComposerLLMCallStatus.SUCCESS) or (outcome.audit_call is not self.call):\n            raise AuditIntegrityError('V4 classifier finalization requires its one actual paid success and original audit')\n        sql, projection = self.required_work.reserve_pair(RequiredWorkSource.PROVIDER_SETTLEMENT_SQL, RequiredWorkSource.PROVIDER_SETTLEMENT_PROJECTION)\n        try:\n            settled, cancellations = await self._join(finalizer(outcome, sql))\n        except BaseException:\n            if sql.complete:\n                projection.complete_without_submission()\n            raise\n        if type(settled) is RequiredSQLRaised:\n            if sql.complete:\n                projection.complete_without_submission()\n            failures: list[BaseException] = [settled.error]\n            for cancellation in (*settled.deferred_cancellations, *cancellations):\n                if all((cancellation is not prior for prior in failures)):\n                    failures.append(cancellation)\n            if len(failures) == 1:\n                raise settled.error\n            raise BaseExceptionGroup('Classifier V4 required SQL and original cancellations', failures)\n        if type(settled) is not RequiredSQLReturned:\n            raise AuditIntegrityError('V4 classifier finalizer lost its actual required SQL outcome')\n        result = settled.value\n        if type(result) not in (ClassifierPrimaryResult, VerifiedClassifierPaidOverflowTerminal):\n            raise AuditIntegrityError('V4 classifier finalizer returned a foreign paid outcome')\n        self._continuation_classifier_v4_terminal = type(result) is VerifiedClassifierPaidOverflowTerminal\n        projection.begin_projection()\n        projection.complete_owned()\n        if self._cost_budget is not None:\n            self._cost_budget.consume_settled(self.call, response_accountable=self._cost_response_accountable)\n        self.continuation_classifier_v4_result = result\n        self.attempt = None\n        self.call = None\n        self._sdk_entered = False\n        deferred: list[asyncio.CancelledError] = []\n        for cancellation in (*settled.deferred_cancellations, *cancellations):\n            if all((cancellation is not prior for prior in deferred)):\n                deferred.append(cancellation)\n        self.continuation_classifier_v4_deferred_cancellations = tuple(deferred)\n\n    async def finalize_continuation_semantic_v4(self, observation: SemanticProviderResponseObservation) -> SemanticV4Finalization:\n        from elspeth.web.composer.continuation_semantic_observation import SemanticCompleteResponseObservation, SemanticIncompleteResponseObservation, SemanticInvalidResponseObservation\n        from elspeth.web.sessions.continuation_semantic_advisor import SemanticAdvisorProviderResult\n        from elspeth.web.sessions.continuation_semantic_paid_overflow_terminal import VerifiedSemanticPaidOverflowTerminal\n        finalizer = self._continuation_semantic_v4_finalizer\n        if type(observation) not in (SemanticCompleteResponseObservation, SemanticIncompleteResponseObservation, SemanticInvalidResponseObservation) or finalizer is None or self.continuation_semantic_v4 is None or (self.continuation_semantic_v4_admission is None) or (self.continuation_semantic_v4_result is not None) or (self.attempt is None) or (not self._sdk_entered) or (self.call is None) or (type(observation) is SemanticCompleteResponseObservation and self.call.status is not ComposerLLMCallStatus.SUCCESS) or (type(observation) is SemanticInvalidResponseObservation and self.call.status is not ComposerLLMCallStatus.MALFORMED_RESPONSE) or (self.call.status not in (ComposerLLMCallStatus.SUCCESS, ComposerLLMCallStatus.MALFORMED_RESPONSE)):\n            raise AuditIntegrityError('V4 semantic finalization requires its one actual returned response and original audit')\n        sql, projection = self.required_work.reserve_pair(RequiredWorkSource.PROVIDER_SETTLEMENT_SQL, RequiredWorkSource.PROVIDER_SETTLEMENT_PROJECTION)\n        try:\n            outcome, cancellations = await self._join(finalizer(observation, sql))\n        except BaseException:\n            if sql.complete:\n                projection.complete_without_submission()\n            raise\n        if type(outcome) is RequiredSQLRaised:\n            if sql.complete:\n                projection.complete_without_submission()\n            failures: list[BaseException] = [outcome.error]\n            for cancellation in (*outcome.deferred_cancellations, *cancellations):\n                if all((cancellation is not prior for prior in failures)):\n                    failures.append(cancellation)\n            if len(failures) == 1:\n                raise outcome.error\n            raise BaseExceptionGroup('Semantic V4 required SQL and original cancellations', failures)\n        if type(outcome) is not RequiredSQLReturned:\n            raise AuditIntegrityError('V4 semantic finalizer lost its actual required SQL outcome')\n        result = outcome.value\n        self._continuation_semantic_v4_sql_committed = True\n        if type(result) not in (SemanticAdvisorProviderResult, VerifiedSemanticPaidOverflowTerminal):\n            raise AuditIntegrityError('V4 semantic finalizer returned a foreign paid outcome')\n        self._continuation_semantic_v4_terminal = type(result) is VerifiedSemanticPaidOverflowTerminal or (type(result) is SemanticAdvisorProviderResult and result.original_error is not None)\n        projection.begin_projection()\n        projection.complete_owned()\n        if self._cost_budget is not None:\n            self._cost_budget.consume_settled(self.call, response_accountable=self._cost_response_accountable)\n        self.continuation_semantic_v4_result = result\n        self.attempt = None\n        self.call = None\n        self._sdk_entered = False\n        deferred: list[asyncio.CancelledError] = []\n        for cancellation in (*outcome.deferred_cancellations, *cancellations):\n            if all((cancellation is not prior for prior in deferred)):\n                deferred.append(cancellation)\n        return SemanticV4Finalization(result, tuple(deferred))\n\n    async def settle(self) -> None:\n        if self._v4_returned_incomplete_committed:\n            return\n        if self.attempt is None:\n            return\n        if self.has_v4_unentered_hold():\n            return\n        if self.continuation_title_v4_admission is not None and self._sdk_entered:\n            return\n        if self.continuation_semantic_v4_admission is not None and self._sdk_entered:\n            return\n        if self.continuation_primary_v4_admission is not None and self._sdk_entered:\n            return\n        if self.continuation_classifier_v4_admission is not None and self._sdk_entered:\n            return\n        if self.continuation_checkpoint_v4_admission is not None and self._sdk_entered:\n            return\n        if not self._sdk_entered or self.call is None:\n            raise AuditIntegrityError('Required provider attempt lacks complete terminal audit')\n        if self.continuation_title is not None:\n            raise AuditIntegrityError('Continuation TITLE settlement requires its original V4 admission')\n        binding = self.required_work\n        projection: RequiredWorkTicket | None = None\n        if binding.role is RequiredWorkRole.TITLE:\n            sql = binding.coordinator.reserve(RequiredWorkSource.TITLE_PROVIDER_SETTLEMENT_SQL, transition_ordinal=binding.transition_ordinal, semantic_ordinal=binding.semantic_ordinal, recurrence_ordinal=self._settlements)\n            self._settlements += 1\n        else:\n            sql, projection = binding.reserve_pair(RequiredWorkSource.PROVIDER_SETTLEMENT_SQL, RequiredWorkSource.PROVIDER_SETTLEMENT_PROJECTION)\n        try:\n            _, cancellations = await self._join(self.service.finish_provider_attempt(session_operation_context=self.context, call=self.call, required_work=sql))\n        except BaseException:\n            if projection is not None and sql.complete:\n                projection.complete_without_submission()\n            raise\n        if projection is not None:\n            projection.begin_projection()\n            projection.complete_owned()\n        if self._cost_budget is not None:\n            if self._cost_budget.snapshot().applicability == 'continuation_v2' and (self.call.cost_response_accountable is None or self.call.cost_response_accountable is not self._cost_response_accountable):\n                raise AuditIntegrityError('Continuation settlement lost its observed response accountability')\n            self._cost_budget.consume_settled(self.call, response_accountable=self._cost_response_accountable)\n        self.attempt = None\n        self.call = None\n        self._sdk_entered = False\n        self.raise_deferred_cancellations(cancellations)",
    "elspeth.web.composer.continuation_title_provider:run_prepared_title_provider": "async def run_prepared_title_provider(*, prepared: PreparedTitleProvider, invocation_owner: ProviderInvocationOwner) -> TitleAuditSettlementRequest | VerifiedTitlePaidOverflowTerminal | TitlePaidIncompleteResult | TitleNoSdkTerminalResult | SdkTerminalResult | SdkTerminalFailure | None:\n    if type(prepared) is not PreparedTitleProvider or type(invocation_owner) is not ProviderInvocationOwner:\n        raise AuditIntegrityError('TITLE execution requires its actual prepared/current invocation owners')\n    if invocation_owner.required_work is not prepared.work or invocation_owner.service is not _sessions(prepared.composer):\n        raise AuditIntegrityError('TITLE execution changed its actual original work/service')\n    root = decode_continuation_snapshot(prepared.selected.payload_json)\n    decode_continuation_policy(root.admission_policy_json, root.policy_hash)\n    custody = invocation_owner.mint(ProviderInvocationFamily.TITLE)\n    custody.bind_continuation_title(prepared)\n    no_sdk_issuer = TitleNoSdkIssuer(prepared)\n    custody.bind_title_no_sdk_issuer(no_sdk_issuer)\n    settings = prepared.composer._settings\n    request = prepared.request\n    from elspeth.web.composer.continuation_bootstrap import bootstrap_values, decode_continuation_bootstrap\n    from elspeth.web.sessions.continuation_title_provider import _chunk\n\n    def original_user() -> str:\n        with locked_session_transaction(prepared.authority._engine, str(prepared.running.claim.session_id)) as conn:\n            _current(conn, prepared.running, prepared.selected)\n            frame = decode_continuation_snapshot(prepared.selected.payload_json).frames[0]\n            if type(frame) is not InitializeFrame or frame.bootstrap is None:\n                raise AuditIntegrityError('TITLE producer lost its original bootstrap')\n            values = bootstrap_values(decode_continuation_bootstrap(_chunk(conn, prepared.running, frame.bootstrap)))\n            original_request = values.request\n            if type(original_request) is not SendMessageRequest:\n                raise AuditIntegrityError('TITLE producer lost its actual original SendMessage request')\n            return original_request.content\n    user = await _registered(work=prepared.work, source=RequiredWorkSource.CONTINUATION_READ_SQL, operation=original_user, expected=str)\n    from elspeth.web.sessions.continuation_title_provider import V4TitleProviderAdmission\n\n    async def finish_returned_incomplete(incomplete: V4ProviderResultIncomplete, sql: RequiredWorkTicket) -> RequiredSQLFinishOnce[VerifiedPaidIncompleteTerminal]:\n        admission = custody.continuation_title_v4_admission\n        if type(incomplete) is not V4ProviderResultIncomplete or type(admission) is not V4TitleProviderAdmission:\n            raise AuditIntegrityError('V4 TITLE returned-incomplete finalizer lost its original paid admission')\n        names = await current_title_names(service=prepared.composer, running=prepared.running, work=prepared.work)\n        return await run_required_sql_finish_once(sql, _sessions(prepared.composer).settle_v4_title_paid_incomplete_sync, prepared=prepared, admission=admission, incomplete=incomplete, custody=custody, names=names)\n    custody.bind_v4_returned_incomplete_finalizer(finish_returned_incomplete)\n\n    async def finish_sdk(observation: V4SdkFailureObservation, sql: RequiredWorkTicket) -> RequiredSQLFinishOnce[VerifiedSdkTerminal]:\n        owner = prepared.work.coordinator.sql_deadline_owner\n        if owner is not None:\n            owner.begin_settlement()\n        outcome = await _sessions(prepared.composer).finalize_v4_sdk_terminal(observation=observation, custody=custody, required_work=sql)\n        prepared.work.coordinator.validate_provider_settlement_sql_outcome(ticket=sql, actual_outcome=outcome)\n        return outcome\n    custody.bind_v4_sdk_finalizer(finish_sdk)\n    capture = _AutoTitleCapture()\n    try:\n        await _maybe_auto_title_session_required(service=_sessions(prepared.composer), session_id=prepared.running.claim.session_id, model=request.kwargs['model'], temperature=request.kwargs.get('temperature'), seed=request.kwargs.get('seed'), session_operation_context=prepared.running.session_operation_context, kwargs=request.kwargs, audit_messages=request.audit_messages, provider_custody=custody, prompt_guard_mount=prepared.composer.composer_prompt_guard_mount, prompt_guard_mode=settings.composer_prompt_guard_mode, user_message=user, max_prepared_request_bytes=settings.composer_max_prepared_request_bytes, prepared_input=prepared.provider_input, _outcome=capture)\n    except V4SdkTerminalCommitted as control:\n        if custody.v4_sdk_terminal_result is not control.finalization:\n            raise AuditIntegrityError('TITLE SDK result changed original committed custody') from control\n        finalized = control.finalization\n        return SdkTerminalResult(finalized.verified, SdkTerminalFamily.TITLE, finalized.deferred_cancellations)\n    except V4ProviderResultIncomplete as incomplete:\n        result = await custody.finalize_v4_returned_incomplete(incomplete)\n        return TitlePaidIncompleteResult(result.verified, result.deferred_cancellations)\n    except BaseException as original:\n        exceptional = capture_sdk_terminal_failure(original, custody=custody)\n        if exceptional is not None:\n            return exceptional\n        terminal = await _close_v4_title_no_sdk(prepared=prepared, custody=custody, issuer=no_sdk_issuer, error=original)\n        if terminal is not None:\n            return terminal\n        raise\n    if capture.handled_error is not None:\n        exceptional = capture_sdk_terminal_failure(capture.handled_error, custody=custody)\n        if exceptional is not None:\n            return exceptional\n        terminal = await _close_v4_title_no_sdk(prepared=prepared, custody=custody, issuer=no_sdk_issuer, error=capture.handled_error)\n        if terminal is not None:\n            return terminal\n    if custody.continuation_title_v4_terminal is not None:\n        return custody.continuation_title_v4_terminal\n    return custody.continuation_title_result",
    # V1 _run_composer_turn was deleted; the live driver and PRIMARY receipts below own its surviving mint flow.
    "elspeth.web.composer.service:ComposerServiceImpl._new_primary_custody": "def _new_primary_custody(self, provider_owner: ProviderInvocationOwner | None) -> ProviderCallCustody | None:\n    if provider_owner is None:\n        return None\n    if type(provider_owner) is not ProviderInvocationOwner or provider_owner.service is not self._require_sessions_service():\n        raise AuditIntegrityError('Primary invocation owner belongs to another exact service')\n    return provider_owner.mint(ProviderInvocationFamily.PRIMARY)",
    "elspeth.web.composer.advisor_checkpoint:AdvisorCheckpointOwner._new_advisor_custody": "def _new_advisor_custody(self, provider_owner: ProviderInvocationOwner) -> ProviderCallCustody:\n    if type(provider_owner) is not ProviderInvocationOwner or provider_owner.service is not self._require_sessions_service():\n        raise AuditIntegrityError('Advisor invocation owner belongs to another exact service')\n    return provider_owner.mint(ProviderInvocationFamily.ADVISOR)",
    "elspeth.web.execution.routes:create_execution_router.evaluate_run_diagnostics": "@router.post('/api/runs/{run_id}/diagnostics/evaluate', response_model=RunDiagnosticsEvaluationResponse)\nasync def evaluate_run_diagnostics(run_id: UUID, request: Request, user: Annotated[UserIdentity, Depends(require_pipeline_user)], limit: int=Query(50, ge=1, le=100), service: ExecutionService=Depends(_get_execution_service)) -> RunDiagnosticsEvaluationResponse:\n    \"\"\"Ask the configured LLM to explain the current diagnostics snapshot.\"\"\"\n    run = await _verify_run_ownership(run_id, user, request)\n    rate_limiter = await get_rate_limiter(request)\n    await rate_limiter.check(user.user_id)\n    try:\n        status = await _load_run_status_with_accounting(run_id, app=request.app, service=service)\n    except _RunStatusNotFoundError:\n        raise _run_not_found_http() from None\n    except (ValidationError, _RunStatusIntegrityError) as exc:\n        raise _run_integrity_http(exc) from exc\n    try:\n        diagnostics = await run_sync_in_worker(load_run_diagnostics_for_settings, request.app.state.settings, run_id=status.run_id, landscape_run_id=status.landscape_run_id, run_status=status.status, cancel_requested=status.cancel_requested, limit=limit)\n    except RunDiagnosticsAuditUnavailableError as exc:\n        raise HTTPException(status_code=503, detail={'error_type': 'run_diagnostics_audit_unavailable', 'landscape_run_id': exc.landscape_run_id, 'audit_location': exc.audit_location}) from exc\n    composer: ComposerService = request.app.state.composer_service\n    settings: WebSettings = request.app.state.settings\n    recorder = BufferingRecorder()\n    audit_authority = RunDiagnosticsAuditAuthority(run_id=run.id, session_id=run.session_id, state_id=run.state_id)\n\n    async def _persist_diagnostics_llm_calls(*, plugin_crash_pending: bool) -> None:\n        compose_lock = await _get_session_compose_lock_registry(request).get_lock(str(run.session_id))\n        async with compose_lock:\n            await _persist_run_diagnostics_llm_calls(request.app.state.session_service, audit_authority, recorder.llm_calls, plugin_crash_pending=plugin_crash_pending)\n    try:\n        session_service: SessionServiceProtocol = request.app.state.session_service\n        async with await SessionOperationLease.acquire(session_service.session_operation_authority, session_id=run.session_id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id=session_service.session_operation_owner_instance_id, lease_seconds=session_service.session_operation_lease_seconds) as diagnostics_lease:\n            coordinator = RequiredWorkCoordinator(RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, diagnostics_lease.context, invocation_id=str(uuid4())))\n            owner = ProviderInvocationOwner(service=session_service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))\n            try:\n                explanation = await composer.explain_run_diagnostics(llm_safe_diagnostics_snapshot(diagnostics), recorder=recorder, session_operation_context=diagnostics_lease.context, provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY))\n            finally:\n                coordinator.assert_completed()\n    except PromptGuardRefused as exc:\n        await _persist_diagnostics_llm_calls(plugin_crash_pending=True)\n        raise HTTPException(status_code=exc.http_status, detail=exc.to_payload()) from exc\n    except _BadRequestLLMError as exc:\n        await _persist_diagnostics_llm_calls(plugin_crash_pending=True)\n        raise HTTPException(status_code=502, detail=_litellm_error_detail('run_diagnostics_explanation_failed', exc, expose_provider_error=settings.composer_expose_provider_errors)) from exc\n    except ComposerContextUnsupported as exc:\n        await _persist_diagnostics_llm_calls(plugin_crash_pending=False)\n        raise HTTPException(status_code=exc.http_status, detail=exc.to_payload()) from exc\n    except ComposerAdmissionRefused as exc:\n        if isinstance(exc, CredentialMaterialRefused):\n            raise HTTPException(status_code=422, detail=exc.to_payload()) from exc\n        raise HTTPException(status_code=403, detail={'error_type': 'composer_admission_refused', 'failure_code': 'admission_refused', 'detail': str(exc)}) from exc\n    except ComposerServiceError as exc:\n        await _persist_diagnostics_llm_calls(plugin_crash_pending=True)\n        raise HTTPException(status_code=502, detail={'error_type': 'run_diagnostics_explanation_failed', 'detail': str(exc)}) from exc\n    except BaseException:\n        await _persist_diagnostics_llm_calls(plugin_crash_pending=True)\n        raise\n    else:\n        try:\n            await _persist_diagnostics_llm_calls(plugin_crash_pending=False)\n        except RunDiagnosticsAuthorityLostError as lost:\n            raise HTTPException(status_code=409, detail={'error_type': 'run_diagnostics_audit_authority_lost', 'detail': f'Run diagnostics audit authority lost ({lost.reason}); the explanation was discarded because its audit row could not be written.'}) from lost\n    explanation, working_view = _parse_run_diagnostics_working_view(explanation, diagnostics)\n    return RunDiagnosticsEvaluationResponse(run_id=status.run_id, generated_at=datetime.now(UTC), explanation=explanation, working_view=working_view)",
    "elspeth.web.sessions.continuation_driver:_SegmentDriver.__init__": """
def __init__(self, worker: ComposerAsyncWorker, services: ComposerAppServices, running: ComposerOperationRunning, lease: SessionOperationLease):
    if type(services.composer_service) is not ComposerServiceImpl or type(lease.required_work) is not RequiredWorkCoordinator:
        raise AuditIntegrityError('V2 driver requires its actual Composer and required-work owners')
    if lease.context is not running.session_operation_context:
        raise AuditIntegrityError('V2 driver changed its exact started context')
    self.worker, self.services, self.running, self.lease = (worker, services, running, lease)
    self.service = services.composer_service
    self.authority = services.composer_operation_authority
    self.work = RequiredWorkBinding(lease.required_work, 0, 0, RequiredWorkRole.TURN, running)
    self.provider = ProviderInvocationOwner(service=services.session_service, required_work=self.work)
    self.phases = ContinuationLivePhases(self.service, self.authority, self.work, self.provider, lease)
    sql_deadline = lease.required_work.sql_deadline_owner
    if sql_deadline is None:
        raise AuditIntegrityError('V2 driver lost its actual SQL timing owner')
    self.sql_deadline = sql_deadline
    self._consumed_native: list[BaseException] = []
    self._committed_failure: CommittedContinuationFailure | None = None
    self._committed_classifier_prephysical: CommittedClassifierPrephysicalFailure | None = None
    self._action_ordinal = 0
    self._diagnostic_stage = "initialization"
    self.progress_owner: ContinuationProgressOwner | None = None
    self._progress_cleanup_failures: list[BaseException] = []
    self._iteration_stage: IterationStageData | None = None
    self._action_clock: ContinuationActionClock | None = None
    self._handled_reply_deadlines: list[_HandledReplyDeadline] = []
""",
}


# Reviewed current issuer, original SQL writer and cold terminal declarations.
# These are source-local obligations; only three original reserve Call IDs are returned.
V4_NO_SDK_SOURCE_CONTRACTS = {
    "src/elspeth/web/composer/continuation_primary.py:_run_v4_primary_after_prepared": "async def _run_v4_primary_after_prepared(*, service: ComposerServiceImpl, authority: ComposerAsyncOperationAuthority, selected: SelectedContinuationState, prepared: PreparedContinuationProvider, invocation: PreparedPrimaryInvocation, recorder: BufferingRecorder, drained: RecorderDrainCursors, required_work: RequiredWorkBinding, provider_owner: ProviderInvocationOwner, issuer: PrimaryNoSdkIssuer, clock: ContinuationActionClock, deadline: float, env_ref_names: frozenset[str], on_sdk_entry: Callable[[], None] | None) -> PrimaryProviderResult | PrimaryPaidOverflowTerminal | PrimaryPaidIncompleteTerminal | PrimarySdkTerminal | SdkTerminalFailure | PrimaryNoSdkRefusalReceipt:\n    \"\"\"Keep one pre-SDK H and one in-scope paid observation on the same custody.\"\"\"\n    custody: ProviderCallCustody | None = None\n    sessions = service._require_sessions_service()\n    if type(sessions) is not SessionServiceImpl:\n        raise AuditIntegrityError('V4 PRIMARY result lost its original session service')\n    try:\n        custody = provider_owner.mint(ProviderInvocationFamily.PRIMARY)\n        custody.bind_continuation_primary_clock(clock)\n        custody.bind_primary_no_sdk_issuer(issuer)\n        frame = selected.stack.frames[-1]\n        if type(frame) is not ComposeFrame:\n            raise AuditIntegrityError('V4 PRIMARY dispatch lost its original compose frame')\n        by_ref = {chunk.reference: chunk for chunk in selected.chunks}\n        previous = decode_turn_evidence(by_ref[frame.evidence], env_ref_names=env_ref_names, source_chunks=selected.chunks)\n        state = _state_from_tool_json(by_ref[frame.state].canonical, env_ref_names=env_ref_names)\n        now = time.monotonic()\n        remaining = deadline - now\n        deadline_owner = required_work.coordinator.sql_deadline_owner\n        if deadline_owner is not None:\n            remaining = min(remaining, deadline_owner.remaining_dispatch(monotonic_now=now))\n        if remaining <= 0:\n            raise TimeoutError('V4 PRIMARY action allowance expired before provider admission')\n        provider_input = invocation.request.provider_input\n        service._provider_gateway.verify_input(provider_input)\n        if canonical_json(invocation.messages) != canonical_json(provider_input.evidence.messages) or canonical_json(invocation.tools) != canonical_json(provider_input.tools if provider_input.tools is not None else ()):\n            raise AuditIntegrityError('V4 PRIMARY invocation changed its original admitted wire')\n\n        async def finalizer(outcome: _CallModelOutcome, sql: RequiredWorkTicket) -> RequiredSQLFinishOnce[PrimaryProviderResult | PrimaryPaidOverflowTerminal]:\n            admission = custody.continuation_primary_v4_admission\n            if type(admission) is not V4PrimaryProviderAdmission:\n                raise AuditIntegrityError('V4 PRIMARY result lost its exact admitted H')\n            witness = outcome.completion.provider_metadata.v4_completeness\n            if type(witness) is not V4ProviderResultCompleteness:\n                raise AuditIntegrityError('V4 PRIMARY result lacks its original complete SDK return')\n            custody.require_v4_result_completeness(witness)\n            snapshot = recorder.snapshot_for_continuation(drained=drained)\n            observation = observe_v4_primary_provider_result(outcome=outcome, recorder=snapshot, previous_evidence=previous, context=issuer.running.session_operation_context, env_ref_names=env_ref_names)\n            return await sessions.finalize_v4_continuation_primary(prepared=prepared, admission=admission, outcome=outcome, recorder=snapshot, observation=observation, env_ref_names=env_ref_names, required_work=sql)\n\n        async def incomplete_finalizer(incomplete: V4ProviderResultIncomplete, sql: RequiredWorkTicket) -> RequiredSQLFinishOnce[VerifiedPaidIncompleteTerminal]:\n            admission = custody.continuation_primary_v4_admission\n            if type(admission) is not V4PrimaryProviderAdmission or custody.call is None:\n                raise AuditIntegrityError('V4 PRIMARY incomplete result lost its admitted physical source')\n            custody.require_v4_result_incomplete(incomplete)\n            return await sessions.finalize_v4_continuation_primary_incomplete(prepared=prepared, admission=admission, incomplete=incomplete, call=custody.call, required_work=sql)\n\n        async def sdk_finalizer(observed: V4SdkFailureObservation, sql: RequiredWorkTicket) -> RequiredSQLFinishOnce[VerifiedSdkTerminal]:\n            admission = custody.continuation_primary_v4_admission\n            if type(admission) is not V4PrimaryProviderAdmission:\n                raise AuditIntegrityError('PRIMARY SDK terminal lost its original admitted source')\n            return await sessions.finalize_v4_continuation_primary_sdk_terminal(prepared=prepared, admission=admission, observation=observed, custody=custody, required_work=sql)\n        custody.bind_continuation_primary_v4(prepared, finalizer)\n        custody.bind_v4_returned_incomplete_finalizer(incomplete_finalizer)\n        custody.bind_v4_sdk_finalizer(sdk_finalizer)\n        actual = await service._provider_gateway._call_llm_outcome_with_audit(provider_input.evidence.messages, provider_input.tools if provider_input.tools is not None else [], timeout=remaining, recorder=recorder, provider_custody=custody, credential_state=state, continuation_input_sha256=invocation.request.input_sha256, prepared_input=provider_input, on_sdk_entry=on_sdk_entry)\n    except V4SdkTerminalCommitted as control:\n        if custody is None or custody.v4_sdk_terminal_result is not control.finalization:\n            raise AuditIntegrityError('PRIMARY SDK control changed its original committed custody') from control\n        sdk_finalized = control.finalization\n        return PrimarySdkTerminal(sdk_finalized.verified.receipt, sdk_finalized.verified.terminal, sdk_finalized.deferred_cancellations)\n    except V4PaidIncompleteCommitted as control:\n        if custody is None:\n            raise AuditIntegrityError('V4 PRIMARY incomplete control lost its original custody') from control\n        finalization = custody.require_v4_paid_incomplete_committed(control)\n        return PrimaryPaidIncompleteTerminal(finalization.verified.receipt, finalization.verified.terminal, finalization.deferred_cancellations)\n    except BaseException as error:\n        if custody is not None:\n            committed_failure = capture_sdk_terminal_failure(error, custody=custody)\n            if committed_failure is not None:\n                return committed_failure\n        failure = _pre_sdk_failure(error, preparing=False)\n        if custody is not None and custody.continuation_primary_v4_admission is not None and custody.has_v4_unentered_hold():\n            if failure is not None:\n                return await _finish_v4_primary_no_sdk(service=service, authority=authority, required_work=required_work, issuer=issuer, custody=custody, admission=custody.continuation_primary_v4_admission, reason=failure[0], original_failure=failure[1], original_cancellations=failure[2])\n        elif issuer.can_refuse() and failure is not None:\n            return await _finish_primary_no_sdk(authority=authority, required_work=required_work, issuer=issuer, reason=failure[0], original_failure=failure[1], original_cancellations=failure[2])\n        raise\n    result = custody.continuation_primary_v4_result\n    if type(result) is PrimaryProviderResult:\n        if result.outcome is not actual:\n            raise AuditIntegrityError('V4 PRIMARY complete result changed its original paid outcome')\n    elif type(result) is PrimaryPaidOverflowTerminal:\n        admission = custody.continuation_primary_v4_admission\n        if type(admission) is not V4PrimaryProviderAdmission or result.receipt.identity.attempt_id != admission.attempt.attempt_id:\n            raise AuditIntegrityError('V4 PRIMARY paid terminal changed its original attempt')\n    else:\n        raise AuditIntegrityError('V4 PRIMARY provider returned without its exact SQL result')\n    return replace(result, deferred_cancellations=custody.continuation_primary_v4_deferred_cancellations)",
    "src/elspeth/web/composer/continuation_primary.py:_finish_v4_primary_no_sdk": 'async def _finish_v4_primary_no_sdk(\n    *,\n    service: ComposerServiceImpl,\n    authority: ComposerAsyncOperationAuthority,\n    required_work: RequiredWorkBinding,\n    issuer: PrimaryNoSdkIssuer,\n    custody: ProviderCallCustody,\n    admission: V4PrimaryProviderAdmission,\n    reason: PrimaryNoSdkRefusalReason,\n    original_failure: BaseException,\n    original_cancellations: tuple[asyncio.CancelledError, ...] = (),\n) -> PrimaryNoSdkRefusalReceipt:\n    """Dispose the actual admitted, unentered H and physical attempt together."""\n    deadline_owner = required_work.coordinator.sql_deadline_owner\n    if deadline_owner is not None:\n        deadline_owner.begin_settlement()\n    permit = issuer.reserve_v4_refusal(admission=admission, reason=reason, original_failure=original_failure)\n    sessions = service._require_sessions_service()\n    if type(sessions) is not SessionServiceImpl:\n        raise AuditIntegrityError("V4 PRIMARY zero-use refusal lost its original session service")\n    cancellations: list[asyncio.CancelledError] = []\n    if type(original_failure) is asyncio.CancelledError:\n        cancellations.append(original_failure)\n    cancellations.extend(item for item in original_cancellations if all(item is not prior for prior in cancellations))\n    last_failure: BaseException | None = None\n    receipt: PrimaryNoSdkRefusalReceipt | None = None\n    for attempt in range(2):\n        ticket = required_work.coordinator.reserve(\n            RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL,\n            transition_ordinal=required_work.transition_ordinal,\n            semantic_ordinal=required_work.semantic_ordinal,\n            recurrence_ordinal=attempt,\n        )\n        outcome = await sessions.cancel_v4_continuation_primary_unentered(\n            prepared=permit.prepared,\n            admission=admission,\n            permit=permit,\n            required_work=ticket,\n        )\n        required_work.coordinator.validate_undispatched_cancellation_sql_outcome(ticket=ticket, actual_outcome=outcome)\n        if type(outcome) is RequiredSQLReturned:\n            if type(outcome.value) is not PrimaryNoSdkRefusalReceipt:\n                raise AuditIntegrityError("V4 PRIMARY zero-use SQL returned a foreign terminal")\n            receipt = outcome.value\n            cancellations.extend(item for item in outcome.deferred_cancellations if all(item is not prior for prior in cancellations))\n            break\n        if type(outcome) is not RequiredSQLRaised:\n            raise AuditIntegrityError("V4 PRIMARY zero-use SQL lost its finish-once outcome")\n        last_failure = outcome.error\n        cancellations.extend(item for item in outcome.deferred_cancellations if all(item is not prior for prior in cancellations))\n        read_sql, read_projection = required_work.reserve_pair(\n            RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION\n        )\n        observed = await run_required_sql_finish_once(read_sql, authority.read_continuation_primary_no_sdk_refusal, permit)\n        readback, read_failure, read_deferred = _project_no_sdk_sql(\n            required_work, read_sql, read_projection, observed, expected=PrimaryNoSdkRefusalReceipt, allow_none=True\n        )\n        cancellations.extend(item for item in read_deferred if all(item is not prior for prior in cancellations))\n        if read_failure is not None:\n            raise BaseExceptionGroup("V4 PRIMARY zero-use refusal and readback failed", [last_failure, read_failure])\n        if readback is not None:\n            receipt = readback\n            break\n    if receipt is None:\n        if last_failure is not None:\n            raise last_failure\n        raise AuditIntegrityError("V4 PRIMARY zero-use refusal lost its exact terminal")\n    # A returned SQL result is still verified cold before in-memory custody is closed.\n    read_sql, read_projection = required_work.reserve_pair(\n        RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION\n    )\n    cold = await run_required_sql_finish_once(read_sql, authority.read_continuation_primary_no_sdk_refusal, permit)\n    proved, failure, deferred = _project_no_sdk_sql(\n        required_work, read_sql, read_projection, cold, expected=PrimaryNoSdkRefusalReceipt, allow_none=True\n    )\n    cancellations.extend(item for item in deferred if all(item is not prior for prior in cancellations))\n    if failure is not None:\n        raise failure\n    if proved is None or proved != receipt:\n        raise AuditIntegrityError("V4 PRIMARY zero-use refusal changed its exact cold terminal")\n    issuer.mark_refusal_committed(permit)\n    custody.note_v4_primary_no_sdk(proved, admission)\n    return replace(proved, deferred_cancellations=tuple(cancellations))',
    "src/elspeth/web/composer/continuation_primary.py:_project_no_sdk_sql": 'def _project_no_sdk_sql[T](\n    work: RequiredWorkBinding,\n    sql: RequiredWorkTicket,\n    projection: RequiredWorkTicket,\n    outcome: RequiredSQLFinishOnce[Any],\n    *,\n    expected: type[T],\n    allow_none: bool = False,\n) -> tuple[T | None, BaseException | None, tuple[asyncio.CancelledError, ...]]:\n    try:\n        work.coordinator.validate_continuation_sql_outcome(\n            ticket=sql,\n            expected_source=RequiredWorkSource.CONTINUATION_STATE_SQL if not allow_none else RequiredWorkSource.CONTINUATION_READ_SQL,\n            actual_outcome=outcome,\n        )\n    except BaseException:\n        projection.complete_without_submission()\n        raise\n    if type(outcome) is RequiredSQLRaised:\n        projection.complete_without_submission()\n        return None, outcome.error, outcome.deferred_cancellations\n    if type(outcome) is not RequiredSQLReturned:\n        projection.complete_without_submission()\n        raise AuditIntegrityError("PRIMARY no-SDK SQL returned an unfamiliar finish-once result")\n    projection.begin_projection()\n    try:\n        value = outcome.value\n        if type(value) is not expected and (not allow_none or value is not None):\n            raise AuditIntegrityError("PRIMARY no-SDK SQL returned a foreign receipt")\n    except BaseException as error:\n        projection.complete_owned(error)\n        raise\n    projection.complete_owned()\n    return value, None, outcome.deferred_cancellations',
    "src/elspeth/web/composer/continuation_title_provider.py:run_prepared_title_provider": "async def run_prepared_title_provider(*, prepared: PreparedTitleProvider, invocation_owner: ProviderInvocationOwner) -> TitleAuditSettlementRequest | VerifiedTitlePaidOverflowTerminal | TitlePaidIncompleteResult | TitleNoSdkTerminalResult | SdkTerminalResult | SdkTerminalFailure | None:\n    if type(prepared) is not PreparedTitleProvider or type(invocation_owner) is not ProviderInvocationOwner:\n        raise AuditIntegrityError('TITLE execution requires its actual prepared/current invocation owners')\n    if invocation_owner.required_work is not prepared.work or invocation_owner.service is not _sessions(prepared.composer):\n        raise AuditIntegrityError('TITLE execution changed its actual original work/service')\n    root = decode_continuation_snapshot(prepared.selected.payload_json)\n    decode_continuation_policy(root.admission_policy_json, root.policy_hash)\n    custody = invocation_owner.mint(ProviderInvocationFamily.TITLE)\n    custody.bind_continuation_title(prepared)\n    no_sdk_issuer = TitleNoSdkIssuer(prepared)\n    custody.bind_title_no_sdk_issuer(no_sdk_issuer)\n    settings = prepared.composer._settings\n    request = prepared.request\n    from elspeth.web.composer.continuation_bootstrap import bootstrap_values, decode_continuation_bootstrap\n    from elspeth.web.sessions.continuation_title_provider import _chunk\n\n    def original_user() -> str:\n        with locked_session_transaction(prepared.authority._engine, str(prepared.running.claim.session_id)) as conn:\n            _current(conn, prepared.running, prepared.selected)\n            frame = decode_continuation_snapshot(prepared.selected.payload_json).frames[0]\n            if type(frame) is not InitializeFrame or frame.bootstrap is None:\n                raise AuditIntegrityError('TITLE producer lost its original bootstrap')\n            values = bootstrap_values(decode_continuation_bootstrap(_chunk(conn, prepared.running, frame.bootstrap)))\n            original_request = values.request\n            if type(original_request) is not SendMessageRequest:\n                raise AuditIntegrityError('TITLE producer lost its actual original SendMessage request')\n            return original_request.content\n    user = await _registered(work=prepared.work, source=RequiredWorkSource.CONTINUATION_READ_SQL, operation=original_user, expected=str)\n    from elspeth.web.sessions.continuation_title_provider import V4TitleProviderAdmission\n\n    async def finish_returned_incomplete(incomplete: V4ProviderResultIncomplete, sql: RequiredWorkTicket) -> RequiredSQLFinishOnce[VerifiedPaidIncompleteTerminal]:\n        admission = custody.continuation_title_v4_admission\n        if type(incomplete) is not V4ProviderResultIncomplete or type(admission) is not V4TitleProviderAdmission:\n            raise AuditIntegrityError('V4 TITLE returned-incomplete finalizer lost its original paid admission')\n        names = await current_title_names(service=prepared.composer, running=prepared.running, work=prepared.work)\n        return await run_required_sql_finish_once(sql, _sessions(prepared.composer).settle_v4_title_paid_incomplete_sync, prepared=prepared, admission=admission, incomplete=incomplete, custody=custody, names=names)\n    custody.bind_v4_returned_incomplete_finalizer(finish_returned_incomplete)\n\n    async def finish_sdk(observation: V4SdkFailureObservation, sql: RequiredWorkTicket) -> RequiredSQLFinishOnce[VerifiedSdkTerminal]:\n        owner = prepared.work.coordinator.sql_deadline_owner\n        if owner is not None:\n            owner.begin_settlement()\n        outcome = await _sessions(prepared.composer).finalize_v4_sdk_terminal(observation=observation, custody=custody, required_work=sql)\n        prepared.work.coordinator.validate_provider_settlement_sql_outcome(ticket=sql, actual_outcome=outcome)\n        return outcome\n    custody.bind_v4_sdk_finalizer(finish_sdk)\n    capture = _AutoTitleCapture()\n    try:\n        await _maybe_auto_title_session_required(service=_sessions(prepared.composer), session_id=prepared.running.claim.session_id, model=request.kwargs['model'], temperature=request.kwargs.get('temperature'), seed=request.kwargs.get('seed'), session_operation_context=prepared.running.session_operation_context, kwargs=request.kwargs, audit_messages=request.audit_messages, provider_custody=custody, prompt_guard_mount=prepared.composer.composer_prompt_guard_mount, prompt_guard_mode=settings.composer_prompt_guard_mode, user_message=user, max_prepared_request_bytes=settings.composer_max_prepared_request_bytes, prepared_input=prepared.provider_input, _outcome=capture)\n    except V4SdkTerminalCommitted as control:\n        if custody.v4_sdk_terminal_result is not control.finalization:\n            raise AuditIntegrityError('TITLE SDK result changed original committed custody') from control\n        finalized = control.finalization\n        return SdkTerminalResult(finalized.verified, SdkTerminalFamily.TITLE, finalized.deferred_cancellations)\n    except V4ProviderResultIncomplete as incomplete:\n        result = await custody.finalize_v4_returned_incomplete(incomplete)\n        return TitlePaidIncompleteResult(result.verified, result.deferred_cancellations)\n    except BaseException as original:\n        exceptional = capture_sdk_terminal_failure(original, custody=custody)\n        if exceptional is not None:\n            return exceptional\n        terminal = await _close_v4_title_no_sdk(prepared=prepared, custody=custody, issuer=no_sdk_issuer, error=original)\n        if terminal is not None:\n            return terminal\n        raise\n    if capture.handled_error is not None:\n        exceptional = capture_sdk_terminal_failure(capture.handled_error, custody=custody)\n        if exceptional is not None:\n            return exceptional\n        terminal = await _close_v4_title_no_sdk(prepared=prepared, custody=custody, issuer=no_sdk_issuer, error=capture.handled_error)\n        if terminal is not None:\n            return terminal\n    if custody.continuation_title_v4_terminal is not None:\n        return custody.continuation_title_v4_terminal\n    return custody.continuation_title_result",
    "src/elspeth/web/composer/continuation_title_provider.py:_close_v4_title_no_sdk": 'async def _close_v4_title_no_sdk(\n    *, prepared: PreparedTitleProvider, custody: ProviderCallCustody, issuer: TitleNoSdkIssuer, error: BaseException\n) -> TitleNoSdkTerminalResult | None:\n    from elspeth.web.sessions.continuation_title_no_sdk_terminal import (\n        VerifiedTitleNoSdkTerminal,\n        read_v4_title_no_sdk_terminal_on_connection,\n    )\n    from elspeth.web.sessions.continuation_title_provider import V4TitleProviderAdmission\n\n    reason = _title_no_sdk_reason(error)\n    admission = custody.continuation_title_v4_admission\n    if reason is None or type(admission) is not V4TitleProviderAdmission or not custody.has_v4_unentered_hold():\n        return None\n    custody.require_v4_title_unentered(admission)\n    issuer.note_original_failure(custody, error)\n    permit = issuer.reserve_refusal(reason=reason, original_failure=error)\n    deadline_owner = prepared.work.coordinator.sql_deadline_owner\n    if deadline_owner is not None:\n        deadline_owner.begin_settlement()\n    ticket = prepared.work.coordinator.reserve(\n        RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL,\n        transition_ordinal=prepared.work.transition_ordinal,\n        semantic_ordinal=prepared.work.semantic_ordinal,\n    )\n    outcome = await _sessions(prepared.composer).cancel_v4_continuation_title_unentered(permit=permit, required_work=ticket)\n    prepared.work.coordinator.validate_undispatched_cancellation_sql_outcome(ticket=ticket, actual_outcome=outcome)\n    cancellations = list(outcome.deferred_cancellations)\n    if type(error) is asyncio.CancelledError and all(error is not prior for prior in cancellations):\n        cancellations.insert(0, error)\n    verified: VerifiedTitleNoSdkTerminal | None = None\n    if type(outcome) is RequiredSQLReturned:\n        verified = outcome.value\n    elif type(outcome) is RequiredSQLRaised:\n        read_sql, projection = prepared.work.reserve_pair(\n            RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION\n        )\n\n        def readback() -> VerifiedTitleNoSdkTerminal | None:\n            with locked_session_transaction(prepared.authority._engine, str(prepared.running.claim.session_id)) as conn:\n                return read_v4_title_no_sdk_terminal_on_connection(\n                    conn,\n                    session_id=prepared.running.claim.session_id,\n                    operation_id=prepared.running.claim.operation_id,\n                    names=prepared.names,\n                )\n\n        try:\n            observed = await run_required_sql_finish_once(read_sql, readback)\n            prepared.work.coordinator.validate_continuation_sql_outcome(\n                ticket=read_sql, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=observed\n            )\n        except BaseException:\n            projection.complete_without_submission()\n            raise\n        if type(observed) is RequiredSQLRaised:\n            projection.complete_without_submission()\n            raise BaseExceptionGroup("V4 TITLE no-SDK settlement and cold readback failed", [outcome.error, observed.error])\n        if type(observed) is not RequiredSQLReturned:\n            projection.complete_without_submission()\n            raise AuditIntegrityError("V4 TITLE no-SDK readback lost its owned SQL outcome")\n        projection.begin_projection()\n        try:\n            if observed.value is not None and type(observed.value) is not VerifiedTitleNoSdkTerminal:\n                raise AuditIntegrityError("V4 TITLE no-SDK readback returned a foreign terminal")\n            verified = observed.value\n        except BaseException as projection_error:\n            projection.complete_owned(projection_error)\n            raise\n        projection.complete_owned()\n        cancellations.extend(item for item in observed.deferred_cancellations if all(item is not prior for prior in cancellations))\n        if verified is None:\n            raise outcome.error\n    else:\n        raise AuditIntegrityError("V4 TITLE no-SDK settlement lost its owned SQL outcome")\n    if (\n        type(verified) is not VerifiedTitleNoSdkTerminal\n        or verified.source != admission.hold\n        or verified.provider_attempt_id != admission.attempt.attempt_id\n        or verified.hold_state != "consumed"\n        or verified.reason is not reason\n    ):\n        raise AuditIntegrityError("V4 TITLE no-SDK closure changed its original paid admission")\n    return TitleNoSdkTerminalResult(verified, tuple(cancellations))',
    "src/elspeth/web/sessions/continuation_classifier_primary.py:ClassifierPrimaryIssuer.__init__": "def __init__(self, *, service: ComposerServiceImpl, authority: ComposerAsyncOperationAuthority, running: ComposerOperationRunning, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory, runtime: RestoredContinuationRuntime, required_work: RequiredWorkBinding, provider_owner: ProviderInvocationOwner, clock: ContinuationActionClock) -> None:\n    from elspeth.web.composer.service import ComposerServiceImpl\n    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority\n    if type(service) is not ComposerServiceImpl or type(authority) is not ComposerAsyncOperationAuthority or type(running) is not ComposerOperationRunning or (type(selected) is not SelectedContinuationState) or (type(runtime) is not RestoredContinuationRuntime) or (type(inventory) is not CurrentContinuationEnvInventory) or (type(required_work) is not RequiredWorkBinding) or (type(provider_owner) is not ProviderInvocationOwner) or (type(clock) is not ContinuationActionClock) or (required_work.running is not running) or (provider_owner.required_work is not required_work) or (runtime.source.selected is not selected) or (runtime.materialization.selected is not selected) or (runtime.materialization.inventory is not inventory) or (runtime.materialization.service is not service) or (runtime.materialization.authority is not authority) or (service._session_engine is not authority._engine) or (provider_owner.service is not service._require_sessions_service()) or (inventory.running is not running) or (runtime.bindings.provider_owner is not provider_owner) or (runtime.bindings.required_work is not required_work) or (runtime.bindings.session_operation_context is not running.session_operation_context):\n        raise AuditIntegrityError('classifier PRIMARY issuer crossed its actual live owners')\n    policy = decode_continuation_policy(selected.stack.admission_policy_json, selected.stack.policy_hash)\n    if clock.policy != policy.actions.primary:\n        raise AuditIntegrityError('classifier PRIMARY cannot replace its original admitted action clock')\n    owner = required_work.coordinator.sql_deadline_owner\n    if type(owner) is not ContinuationSQLDeadlineOwner or owner.running is not running or owner.policy != policy:\n        raise AuditIntegrityError('classifier PRIMARY requires its actual original SQL deadline owner')\n    self.service, self.authority, self.running, self.selected = (service, authority, running, selected)\n    self.inventory, self.runtime, self.work, self.provider_owner, self.clock = (inventory, runtime, required_work, provider_owner, clock)\n    self._started = False\n    self._sdk_entered = False\n    self._failure: BaseException | None = None\n    self._result: ClassifierPrimaryResult | None = None\n    self._terminal: VerifiedClassifierPaidOverflowTerminal | VerifiedClassifierPaidIncompleteTerminal | ClassifierNoSdkRefusalReceipt | SdkTerminalResult | SdkTerminalFailure | None = None\n    self._reply_deadline: float | None = None\n    self._reply_deadline_observation: ClassifierReplyDeadline | None = None\n    self._reply_clock = clock\n    self._reply_sql_owner = owner",
    "src/elspeth/web/sessions/continuation_classifier_primary.py:ClassifierPrimaryIssuer._invoke_v4_prepared": "async def _invoke_v4_prepared(self, *, prepared: PreparedClassifierPrimary, request: PreparedContinuationPrimaryRequest, no_sdk_issuer: ClassifierNoSdkIssuer, branch: ClassifierPrimaryBranch, prepared_input: PreparedProviderInput, recorder: BufferingRecorder, deadline: float, credential_state: CompositionState | None) -> ClassifierPrimaryResult | VerifiedClassifierPaidOverflowTerminal | VerifiedClassifierPaidIncompleteTerminal | ClassifierNoSdkRefusalReceipt | SdkTerminalResult | SdkTerminalFailure:\n    from elspeth.web.composer.continuation_primary import _pre_sdk_failure\n    from elspeth.web.composer.provider_quota import V4PaidIncompleteCommitted, V4SdkTerminalCommitted\n    from elspeth.web.composer.provider_result_admission import V4ProviderResultIncomplete\n    from elspeth.web.composer.provider_sdk_observation import V4SdkFailureObservation\n    from elspeth.web.composer.provider_sdk_terminal import capture_sdk_terminal_failure\n    from elspeth.web.sessions.service import SessionServiceImpl\n    sessions = self.service._require_sessions_service()\n    if type(sessions) is not SessionServiceImpl or no_sdk_issuer.prepared_candidate is not prepared:\n        raise AuditIntegrityError('V4 classifier lost its original prepared sessions owner')\n    self.clock.finish_preparation()\n    custody = self.provider_owner.mint(ProviderInvocationFamily.PRIMARY)\n    custody.bind_continuation_primary_clock(self.clock)\n    custody.bind_classifier_no_sdk_issuer(no_sdk_issuer)\n    provider = PreparedClassifierPrimaryProvider(prepared, request)\n\n    async def finalizer(outcome: _CallModelOutcome, sql: RequiredWorkTicket) -> RequiredSQLFinishOnce[ClassifierPrimaryResult | VerifiedClassifierPaidOverflowTerminal]:\n        from elspeth.web.composer.provider_result_admission import V4ProviderResultCompleteness\n        admission = custody.continuation_classifier_v4_admission\n        if type(admission) is not V4ClassifierProviderAdmission:\n            raise AuditIntegrityError('V4 classifier response lost its original admitted physical/H')\n        witness = outcome.completion.provider_metadata.v4_completeness\n        if type(witness) is not V4ProviderResultCompleteness:\n            raise AuditIntegrityError('V4 classifier response lost its actual SDK return witness')\n        custody.require_v4_result_completeness(witness)\n        buffer = recorder.snapshot_for_continuation(drained=self.runtime.drained)\n        permit = _PublishPermit()\n        _returned[permit] = (prepared, admission.dispatched, outcome, buffer)\n        return await sessions.finalize_v4_continuation_classifier(prepared=provider, admission=admission, outcome=outcome, recorder=buffer, permit=permit, required_work=sql)\n    custody.bind_continuation_classifier_v4(provider, finalizer)\n\n    async def incomplete_finalizer(incomplete: V4ProviderResultIncomplete, sql: RequiredWorkTicket) -> RequiredSQLFinishOnce[VerifiedPaidIncompleteTerminal]:\n        admission = custody.continuation_classifier_v4_admission\n        call = custody.call\n        if type(admission) is not V4ClassifierProviderAdmission or type(call) is not ComposerLLMCall:\n            raise AuditIntegrityError('V4 classifier incomplete result lost its admitted physical audit/H')\n        custody.require_v4_result_incomplete(incomplete)\n        return await sessions.finalize_v4_continuation_classifier_incomplete(prepared=provider, admission=admission, incomplete=incomplete, call=call, required_work=sql)\n    custody.bind_v4_returned_incomplete_finalizer(incomplete_finalizer)\n\n    async def sdk_finalizer(observation: V4SdkFailureObservation, sql: RequiredWorkTicket) -> RequiredSQLFinishOnce[VerifiedSdkTerminal]:\n        deadline_owner = self.work.coordinator.sql_deadline_owner\n        if deadline_owner is not None:\n            deadline_owner.begin_settlement()\n        result = await sessions.finalize_v4_sdk_terminal(observation=observation, custody=custody, required_work=sql)\n        self.work.coordinator.validate_provider_settlement_sql_outcome(ticket=sql, actual_outcome=result)\n        return result\n    custody.bind_v4_sdk_finalizer(sdk_finalizer)\n\n    def entered() -> None:\n        self._sdk_entered = True\n    owner = self.work.coordinator.sql_deadline_owner\n    if type(owner) is not ContinuationSQLDeadlineOwner:\n        raise AuditIntegrityError('V4 classifier lost its original SQL allowance')\n    remaining = min(deadline - time.monotonic(), owner.remaining_dispatch(monotonic_now=time.monotonic()))\n    try:\n        if remaining <= 0:\n            raise TimeoutError('V4 classifier original allowance expired before SDK entry')\n        outcome = await self.service._provider_gateway._call_llm_outcome_with_audit(prepared_input.evidence.messages, prepared_input.tools if prepared_input.tools is not None else [], timeout=remaining, recorder=recorder, provider_custody=custody, credential_state=credential_state, continuation_input_sha256=request.input_sha256, prepared_input=prepared_input, on_sdk_entry=entered)\n    except V4SdkTerminalCommitted as control:\n        if custody.v4_sdk_terminal_result is not control.finalization:\n            raise AuditIntegrityError('classifier SDK result changed original committed custody') from control\n        finalized = control.finalization\n        sdk_terminal = SdkTerminalResult(finalized.verified, SdkTerminalFamily.CLASSIFIER, finalized.deferred_cancellations)\n        self._terminal = sdk_terminal\n        return sdk_terminal\n    except V4PaidIncompleteCommitted as control:\n        committed = custody.require_v4_paid_incomplete_committed(control)\n        admission = custody.continuation_classifier_v4_admission\n        verified = committed.verified\n        if type(admission) is not V4ClassifierProviderAdmission or type(verified) is not VerifiedPaidIncompleteTerminal or verified.source != admission.hold or (verified.provider_attempt_id != admission.attempt.attempt_id):\n            raise AuditIntegrityError('V4 classifier incomplete terminal lost its original paid source') from control\n        terminal = VerifiedClassifierPaidIncompleteTerminal(verified, committed.deferred_cancellations)\n        self._terminal = terminal\n        return terminal\n    except BaseException as error:\n        exceptional = capture_sdk_terminal_failure(error, custody=custody)\n        if exceptional is not None:\n            self._terminal = exceptional\n            return exceptional\n        failure = _pre_sdk_failure(error, preparing=False)\n        admission = custody.continuation_classifier_v4_admission\n        if type(admission) is V4ClassifierProviderAdmission and custody.has_v4_unentered_hold() and (failure is not None):\n            return await self._finish_v4_no_sdk(sessions=sessions, custody=custody, issuer=no_sdk_issuer, admission=admission, reason=ClassifierNoSdkRefusalReason(failure[0].value), original_failure=failure[1], original_cancellations=failure[2])\n        raise\n    result = custody.continuation_classifier_v4_result\n    if type(result) is ClassifierPrimaryResult:\n        if result.outcome is not outcome or result.branch != branch:\n            raise AuditIntegrityError('V4 classifier complete result changed original paid outcome or branch')\n        settled = replace(result, deferred_cancellations=custody.continuation_classifier_v4_deferred_cancellations)\n        self._result = settled\n        return settled\n    if type(result) is VerifiedClassifierPaidOverflowTerminal:\n        admission = custody.continuation_classifier_v4_admission\n        if type(admission) is not V4ClassifierProviderAdmission or result.receipt.identity.attempt_id != admission.attempt.attempt_id:\n            raise AuditIntegrityError('V4 classifier terminal changed its original physical attempt')\n        overflow_terminal = replace(result, deferred_cancellations=custody.continuation_classifier_v4_deferred_cancellations)\n        self._terminal = overflow_terminal\n        return overflow_terminal\n    raise AuditIntegrityError('V4 classifier returned without its exact complete or terminal SQL result')",
    "src/elspeth/web/sessions/continuation_classifier_primary.py:ClassifierPrimaryIssuer._finish_v4_no_sdk": "async def _finish_v4_no_sdk(self, *, sessions: SessionServiceImpl, custody: ProviderCallCustody, issuer: ClassifierNoSdkIssuer, admission: V4ClassifierProviderAdmission, reason: ClassifierNoSdkRefusalReason, original_failure: BaseException, original_cancellations: tuple[asyncio.CancelledError, ...]) -> ClassifierNoSdkRefusalReceipt:\n    from elspeth.web.composer.provider_quota import ProviderCallCustody\n    from elspeth.web.sessions.continuation_classifier_primary_no_sdk import ClassifierNoSdkRefusalReceipt, read_classifier_no_sdk_refusal_on_connection\n    from elspeth.web.sessions.service import SessionServiceImpl\n    if type(sessions) is not SessionServiceImpl or type(custody) is not ProviderCallCustody:\n        raise AuditIntegrityError('V4 classifier no-SDK disposition lost its actual physical custody')\n    owner = self.work.coordinator.sql_deadline_owner\n    if owner is not None:\n        owner.begin_settlement()\n    permit = issuer.reserve_v4_refusal(admission=admission, reason=reason, original_failure=original_failure)\n    cancellations: list[asyncio.CancelledError] = []\n    if type(original_failure) is asyncio.CancelledError:\n        cancellations.append(original_failure)\n    cancellations.extend((item for item in original_cancellations if all((item is not prior for prior in cancellations))))\n\n    async def cold() -> ClassifierNoSdkRefusalReceipt | None:\n\n        def read() -> ClassifierNoSdkRefusalReceipt | None:\n            with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:\n                return read_classifier_no_sdk_refusal_on_connection(conn, session_id=self.running.claim.session_id, operation_id=self.running.claim.operation_id, effect_key=admission.effect_key)\n        sql, projection = self.work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)\n        observed = await run_required_sql_finish_once(sql, read)\n        try:\n            self.work.coordinator.validate_continuation_sql_outcome(ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=observed)\n        except BaseException:\n            projection.complete_without_submission()\n            raise\n        if type(observed) is RequiredSQLRaised:\n            projection.complete_without_submission()\n            raise observed.error\n        if type(observed) is not RequiredSQLReturned:\n            projection.complete_without_submission()\n            raise AuditIntegrityError('V4 classifier no-SDK read lost its finish-once result')\n        projection.begin_projection()\n        if observed.value is not None and type(observed.value) is not ClassifierNoSdkRefusalReceipt:\n            error = AuditIntegrityError('V4 classifier no-SDK read returned a foreign terminal')\n            projection.complete_owned(error)\n            raise error\n        projection.complete_owned()\n        cancellations.extend((item for item in observed.deferred_cancellations if all((item is not prior for prior in cancellations))))\n        return observed.value\n    receipt: ClassifierNoSdkRefusalReceipt | None = None\n    last_failure: BaseException | None = None\n    for recurrence in range(2):\n        ticket = self.work.coordinator.reserve(RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL, transition_ordinal=self.work.transition_ordinal, semantic_ordinal=self.work.semantic_ordinal, recurrence_ordinal=recurrence)\n        outcome = await sessions.cancel_v4_continuation_classifier_unentered(permit=permit, required_work=ticket)\n        self.work.coordinator.validate_undispatched_cancellation_sql_outcome(ticket=ticket, actual_outcome=outcome)\n        cancellations.extend((item for item in outcome.deferred_cancellations if all((item is not prior for prior in cancellations))))\n        if type(outcome) is RequiredSQLReturned:\n            if type(outcome.value) is not ClassifierNoSdkRefusalReceipt:\n                raise AuditIntegrityError('V4 classifier zero-use SQL returned a foreign terminal')\n            receipt = outcome.value\n        elif type(outcome) is RequiredSQLRaised:\n            last_failure = outcome.error\n        else:\n            raise AuditIntegrityError('V4 classifier zero-use SQL lost its finish-once result')\n        proved = await cold()\n        if proved is not None:\n            if receipt is not None and proved != receipt:\n                raise AuditIntegrityError('V4 classifier no-SDK SQL changed its exact cold terminal')\n            receipt = proved\n            break\n    if receipt is None:\n        if last_failure is not None:\n            raise last_failure\n        raise AuditIntegrityError('V4 classifier no-SDK refusal lost its exact terminal')\n    issuer.mark_refusal_committed(permit)\n    custody.note_v4_classifier_no_sdk(receipt, admission)\n    terminal = replace(receipt, deferred_cancellations=tuple(cancellations))\n    self._terminal = terminal\n    return terminal",
    "src/elspeth/web/composer/continuation_primary_no_sdk.py:PrimaryNoSdkIssuer": 'class PrimaryNoSdkIssuer:\n    """A live invocation authority, never reconstructed from cold SQL absence."""\n\n    __slots__ = (\n        "_attempt_id",\n        "_attempt_model",\n        "_cancelled_attempt",\n        "_custody",\n        "_dispatched",\n        "_dispatched_candidate",\n        "_input_sha256",\n        "_lock",\n        "_permit",\n        "_prepared",\n        "_prepared_candidate",\n        "_selected_checkpoint_id",\n        "_selected_cursor",\n        "_state",\n        "clock",\n        "running",\n        "work",\n    )\n\n    def __init__(\n        self,\n        *,\n        running: ComposerOperationRunning,\n        work: RequiredWorkBinding,\n        selected_checkpoint_id: str,\n        selected_cursor: int,\n        input_sha256: str,\n        clock: ContinuationActionClock,\n    ) -> None:\n        if (\n            type(running) is not ComposerOperationRunning\n            or type(work) is not RequiredWorkBinding\n            or work.running is not running\n            or type(clock) is not ContinuationActionClock\n            or type(selected_checkpoint_id) is not str\n            or not selected_checkpoint_id\n            or type(selected_cursor) is not int\n            or selected_cursor < 0\n            or type(input_sha256) is not str\n            or len(input_sha256) != 64\n        ):\n            raise AuditIntegrityError("PRIMARY no-SDK issuer requires the original nominal invocation")\n        self.running = running\n        self.work = work\n        self.clock = clock\n        self._selected_checkpoint_id = selected_checkpoint_id\n        self._selected_cursor = selected_cursor\n        self._input_sha256 = input_sha256\n        self._lock = RLock()\n        self._state = "open"\n        self._prepared: PreparedContinuationProvider | None = None\n        self._prepared_candidate: PreparedContinuationProvider | None = None\n        self._dispatched: StoredContinuationSnapshot | None = None\n        self._dispatched_candidate: StoredContinuationSnapshot | None = None\n        self._custody: ProviderCallCustody | None = None\n        self._attempt_id: str | None = None\n        self._attempt_model: str | None = None\n        self._cancelled_attempt = False\n        self._permit: PrimaryNoSdkRefusalPermit | None = None\n\n    @property\n    def original_checkpoint_id(self) -> str:\n        return self._selected_checkpoint_id\n\n    @property\n    def original_cursor(self) -> int:\n        return self._selected_cursor\n\n    @property\n    def input_sha256(self) -> str:\n        return self._input_sha256\n\n    @property\n    def prepared_candidate(self) -> PreparedContinuationProvider | None:\n        with self._lock:\n            return self._prepared_candidate\n\n    @property\n    def dispatched_candidate(self) -> StoredContinuationSnapshot | None:\n        with self._lock:\n            return self._dispatched_candidate\n\n    def note_prepared_candidate(self, prepared: PreparedContinuationProvider) -> None:\n        with self._lock:\n            if self._state != "open" or self._prepared_candidate is not None or type(prepared) is not PreparedContinuationProvider:\n                raise AuditIntegrityError("PRIMARY preparation candidate is foreign or repeated")\n            self._prepared_candidate = prepared\n\n    def note_dispatched_candidate(self, dispatched: StoredContinuationSnapshot) -> None:\n        with self._lock:\n            if (\n                self._state != "open"\n                or self._prepared is None\n                or self._dispatched_candidate is not None\n                or type(dispatched) is not StoredContinuationSnapshot\n            ):\n                raise AuditIntegrityError("PRIMARY dispatch candidate is foreign or repeated")\n            self._dispatched_candidate = dispatched\n\n    def bind_prepared(self, prepared: PreparedContinuationProvider) -> None:\n        with self._lock:\n            if (\n                self._state != "open"\n                or self._prepared is not None\n                or prepared is not self._prepared_candidate\n                or type(prepared) is not PreparedContinuationProvider\n                or prepared.binding.running is not self.running\n                or prepared.binding.family != 0\n                or prepared.binding.input_sha256 != self._input_sha256\n                or prepared.binding.effect_key != f"provider:0:{prepared.binding.ordinal}"\n                or prepared.snapshot.cursor != self._selected_cursor + 1\n                or prepared.snapshot.checkpoint_id == self._selected_checkpoint_id\n            ):\n                raise AuditIntegrityError("PRIMARY no-SDK issuer changed its actual committed preparation")\n            self._prepared = prepared\n\n    def bind_dispatched(self, dispatched: StoredContinuationSnapshot) -> None:\n        with self._lock:\n            if (\n                self._state != "open"\n                or self._prepared is None\n                or self._dispatched is not None\n                or dispatched is not self._dispatched_candidate\n                or type(dispatched) is not StoredContinuationSnapshot\n                or dispatched.cursor != self._prepared.snapshot.cursor + 1\n                or dispatched.checkpoint_id == self._prepared.snapshot.checkpoint_id\n            ):\n                raise AuditIntegrityError("PRIMARY no-SDK issuer changed its actual committed dispatch")\n            self._dispatched = dispatched\n\n    def bind_cold_recovered_dispatched(self, dispatched: StoredContinuationSnapshot) -> None:\n        """Replace only an equal in-worker candidate after a trusted cold SQL proof."""\n        with self._lock:\n            if (\n                self._state != "open"\n                or self._prepared is None\n                or self._dispatched is not None\n                or type(dispatched) is not StoredContinuationSnapshot\n                or self._dispatched_candidate is None\n                or self._dispatched_candidate != dispatched\n            ):\n                raise AuditIntegrityError("PRIMARY cold admission did not match its original SQL dispatch candidate")\n            self._dispatched_candidate = dispatched\n            self.bind_dispatched(dispatched)\n\n    def bind_custody(self, custody: ProviderCallCustody) -> None:\n        from elspeth.web.composer.continuation_codec import decode_continuation_snapshot\n        from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy\n        from elspeth.web.composer.provider_quota import ProviderCallCustody\n\n        with self._lock:\n            v4_prepared = False\n            if self._prepared is not None:\n                root = decode_continuation_snapshot(self._prepared.snapshot.payload_json)\n                v4_prepared = (\n                    type(decode_continuation_policy(root.admission_policy_json, root.policy_hash)) is ContinuationAdmissionPolicyV4\n                )\n            if (\n                self._state != "open"\n                or self._prepared is None\n                or (self._dispatched is None and not v4_prepared)\n                or self._custody is not None\n                or type(custody) is not ProviderCallCustody\n                or custody.context is not self.running.session_operation_context\n                or custody.continuation_primary_clock is not self.clock\n            ):\n                raise AuditIntegrityError("PRIMARY no-SDK issuer cannot bind a foreign physical custody")\n            self._custody = custody\n\n    def reserve_v4_refusal(\n        self, *, admission: V4PrimaryProviderAdmission, reason: PrimaryNoSdkRefusalReason, original_failure: BaseException\n    ) -> PrimaryNoSdkRefusalPermit:\n        """Reserve a proven unentered admitted attempt before one atomic SQL cancellation."""\n        from elspeth.web.composer.continuation_codec import decode_continuation_snapshot\n        from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy\n        from elspeth.web.sessions.continuation_primary import V4PrimaryProviderAdmission\n\n        with self._lock:\n            if self._prepared is None:\n                raise AuditIntegrityError("V4 PRIMARY no-SDK refusal has no original prepared source")\n            root = decode_continuation_snapshot(self._prepared.snapshot.payload_json)\n            if (\n                type(decode_continuation_policy(root.admission_policy_json, root.policy_hash)) is not ContinuationAdmissionPolicyV4\n                or type(admission) is not V4PrimaryProviderAdmission\n                or self._state != "open"\n                or self._dispatched is None\n                or self._dispatched != admission.selected\n                or self._custody is None\n                or self._attempt_id is None\n                or self._attempt_id != admission.attempt.attempt_id\n                or self._attempt_model is None\n                or self._cancelled_attempt\n                or self._permit is not None\n                or type(reason) is not PrimaryNoSdkRefusalReason\n                or reason\n                in (\n                    PrimaryNoSdkRefusalReason.GUARD_UNSUPPORTED_INPUT,\n                    PrimaryNoSdkRefusalReason.GUARD_BLOCKED,\n                    PrimaryNoSdkRefusalReason.GUARD_UNAVAILABLE,\n                )\n                or (\n                    reason in (PrimaryNoSdkRefusalReason.PREPARATION_EXPIRED, PrimaryNoSdkRefusalReason.PRE_SDK_DEADLINE)\n                    and type(original_failure) is not TimeoutError\n                )\n                or (reason is PrimaryNoSdkRefusalReason.CHARGEABLE_REFUSED and type(original_failure) is not ChargeableAdmissionRefused)\n                or (reason is PrimaryNoSdkRefusalReason.COST_REFUSED and type(original_failure) is not CommonProviderCostRefused)\n                or (reason is PrimaryNoSdkRefusalReason.STORAGE_FAILURE and not isinstance(original_failure, SQLAlchemyError))\n                or (reason is PrimaryNoSdkRefusalReason.INTEGRITY_REFUSED and not isinstance(original_failure, AuditIntegrityError))\n                or (reason is PrimaryNoSdkRefusalReason.CANCELLED_BEFORE_SDK and type(original_failure) is not asyncio.CancelledError)\n                or (\n                    reason is PrimaryNoSdkRefusalReason.RETAINED_TAIL_UNBOUNDED\n                    and type(original_failure) is not ContinuationRetainedTailUnbounded\n                )\n                or (\n                    reason\n                    in (\n                        PrimaryNoSdkRefusalReason.GUARD_UNSUPPORTED_INPUT,\n                        PrimaryNoSdkRefusalReason.GUARD_BLOCKED,\n                        PrimaryNoSdkRefusalReason.GUARD_UNAVAILABLE,\n                    )\n                    and primary_guard_refusal_reason(original_failure) is not reason\n                )\n            ):\n                raise AuditIntegrityError("V4 PRIMARY no-SDK refusal lacks proved unentered admitted custody")\n            self._custody.require_v4_primary_unentered(admission)\n            permit = PrimaryNoSdkRefusalPermit(\n                self,\n                self._prepared,\n                self._dispatched,\n                reason,\n                original_failure,\n                str(uuid4()),\n                self._attempt_id,\n                self._attempt_model,\n            )\n            self._permit = permit\n            self._state = "refusing"\n            return permit\n\n    def note_admitted_attempt(self, custody: ProviderCallCustody, attempt: ProviderAttempt, *, model: str) -> None:\n        with self._lock:\n            if (\n                self._state != "open"\n                or custody is not self._custody\n                or type(attempt) is not ProviderAttempt\n                or type(model) is not str\n                or not model\n                or self._attempt_id is not None\n            ):\n                raise AuditIntegrityError("PRIMARY no-SDK issuer lost its exact admitted attempt")\n            self._attempt_id = attempt.attempt_id\n            self._attempt_model = model\n\n    def note_undispatched_cancel(self, custody: ProviderCallCustody, attempt: ProviderAttempt, *, model: str) -> None:\n        with self._lock:\n            if (\n                self._state != "open"\n                or custody is not self._custody\n                or type(attempt) is not ProviderAttempt\n                or self._attempt_id != attempt.attempt_id\n                or self._attempt_model != model\n                or self._cancelled_attempt\n            ):\n                raise AuditIntegrityError("PRIMARY no-SDK issuer lost its actual undispatched disposition")\n            self._cancelled_attempt = True\n\n    def mark_sdk_entered(self, custody: ProviderCallCustody) -> None:\n        with self._lock:\n            if self._state != "open" or custody is not self._custody or self._dispatched is None:\n                raise AuditIntegrityError("PRIMARY SDK entry conflicts with a pending no-SDK disposition")\n            self._state = "sdk_entered"\n\n    def reserve_refusal(self, *, reason: PrimaryNoSdkRefusalReason, original_failure: BaseException) -> PrimaryNoSdkRefusalPermit:\n        with self._lock:\n            if (\n                self._state != "open"\n                or self._prepared is None\n                or type(reason) is not PrimaryNoSdkRefusalReason\n                or (\n                    reason in (PrimaryNoSdkRefusalReason.PREPARATION_EXPIRED, PrimaryNoSdkRefusalReason.PRE_SDK_DEADLINE)\n                    and type(original_failure) is not TimeoutError\n                )\n                or (reason is PrimaryNoSdkRefusalReason.CHARGEABLE_REFUSED and type(original_failure) is not ChargeableAdmissionRefused)\n                or (reason is PrimaryNoSdkRefusalReason.COST_REFUSED and type(original_failure) is not CommonProviderCostRefused)\n                or (reason is PrimaryNoSdkRefusalReason.STORAGE_FAILURE and not isinstance(original_failure, SQLAlchemyError))\n                or (reason is PrimaryNoSdkRefusalReason.INTEGRITY_REFUSED and not isinstance(original_failure, AuditIntegrityError))\n                or (reason is PrimaryNoSdkRefusalReason.CANCELLED_BEFORE_SDK and type(original_failure) is not asyncio.CancelledError)\n                or (\n                    reason is PrimaryNoSdkRefusalReason.RETAINED_TAIL_UNBOUNDED\n                    and type(original_failure) is not ContinuationRetainedTailUnbounded\n                )\n                or (\n                    reason\n                    in (\n                        PrimaryNoSdkRefusalReason.GUARD_UNSUPPORTED_INPUT,\n                        PrimaryNoSdkRefusalReason.GUARD_BLOCKED,\n                        PrimaryNoSdkRefusalReason.GUARD_UNAVAILABLE,\n                    )\n                    and primary_guard_refusal_reason(original_failure) is not reason\n                )\n                or (self._attempt_id is not None and not self._cancelled_attempt)\n                or self._permit is not None\n            ):\n                raise AuditIntegrityError("PRIMARY no-SDK refusal lacks live unentered settled custody")\n            permit = PrimaryNoSdkRefusalPermit(\n                self,\n                self._prepared,\n                self._dispatched,\n                reason,\n                original_failure,\n                str(uuid4()),\n                self._attempt_id,\n                self._attempt_model,\n            )\n            self._permit = permit\n            self._state = "refusing"\n            return permit\n\n    def can_refuse(self) -> bool:\n        with self._lock:\n            return self._state == "open" and self._prepared is not None and (self._attempt_id is None or self._cancelled_attempt)\n\n    def require_refusal_permit(self, permit: PrimaryNoSdkRefusalPermit) -> None:\n        with self._lock:\n            if self._state not in ("refusing", "committed") or type(permit) is not PrimaryNoSdkRefusalPermit or permit is not self._permit:\n                raise AuditIntegrityError("PRIMARY no-SDK refusal permit is foreign, reused or entered")\n\n    def mark_refusal_committed(self, permit: PrimaryNoSdkRefusalPermit) -> None:\n        with self._lock:\n            self.require_refusal_permit(permit)\n            self._state = "committed"\n\n    def mark_unknown(self) -> None:\n        with self._lock:\n            if self._state == "open":\n                self._state = "unknown"',
    "src/elspeth/web/composer/continuation_primary_no_sdk.py:PrimaryNoSdkRefusalPermit": "@dataclass(frozen=True, slots=True)\nclass PrimaryNoSdkRefusalPermit:\n    issuer: PrimaryNoSdkIssuer\n    prepared: PreparedContinuationProvider\n    dispatched: StoredContinuationSnapshot | None\n    reason: PrimaryNoSdkRefusalReason\n    original_failure: BaseException\n    receipt_key: str\n    undispatched_attempt_id: str | None\n    undispatched_model: str | None",
    "src/elspeth/web/composer/continuation_classifier_no_sdk.py:ClassifierNoSdkIssuer": 'class ClassifierNoSdkIssuer:\n    """Nominal invocation and custody proof, never reconstructed from SQL absence."""\n\n    __slots__ = (\n        "_attempt",\n        "_attempt_model",\n        "_custody",\n        "_dispatched",\n        "_dispatched_candidate",\n        "_input_sha256",\n        "_lock",\n        "_permit",\n        "_prepared",\n        "_prepared_candidate",\n        "_selected_checkpoint_id",\n        "_selected_cursor",\n        "_state",\n        "clock",\n        "running",\n        "work",\n    )\n\n    def __init__(\n        self,\n        *,\n        running: ComposerOperationRunning,\n        work: RequiredWorkBinding,\n        selected_checkpoint_id: str,\n        selected_cursor: int,\n        input_sha256: str,\n        clock: ContinuationActionClock,\n    ) -> None:\n        if (\n            type(running) is not ComposerOperationRunning\n            or type(work) is not RequiredWorkBinding\n            or work.running is not running\n            or type(clock) is not ContinuationActionClock\n            or type(selected_checkpoint_id) is not str\n            or not selected_checkpoint_id\n            or type(selected_cursor) is not int\n            or selected_cursor < 0\n            or type(input_sha256) is not str\n            or len(input_sha256) != 64\n        ):\n            raise AuditIntegrityError("classifier no-SDK issuer requires its original nominal invocation")\n        self.running = running\n        self.work = work\n        self.clock = clock\n        self._selected_checkpoint_id = selected_checkpoint_id\n        self._selected_cursor = selected_cursor\n        self._input_sha256 = input_sha256\n        self._lock = RLock()\n        self._state = "open"\n        self._prepared_candidate: PreparedClassifierPrimary | None = None\n        self._prepared: PreparedClassifierPrimary | None = None\n        self._dispatched_candidate: StoredContinuationSnapshot | None = None\n        self._dispatched: StoredContinuationSnapshot | None = None\n        self._custody: ProviderCallCustody | None = None\n        self._attempt: ProviderAttempt | None = None\n        self._attempt_model: str | None = None\n        self._permit: ClassifierNoSdkRefusalPermit | None = None\n\n    @property\n    def prepared_candidate(self) -> PreparedClassifierPrimary | None:\n        with self._lock:\n            return self._prepared_candidate\n\n    @property\n    def dispatched_candidate(self) -> StoredContinuationSnapshot | None:\n        with self._lock:\n            return self._dispatched_candidate\n\n    @property\n    def original_checkpoint_id(self) -> str:\n        return self._selected_checkpoint_id\n\n    @property\n    def original_cursor(self) -> int:\n        return self._selected_cursor\n\n    @property\n    def input_sha256(self) -> str:\n        return self._input_sha256\n\n    def note_prepared_candidate(self, prepared: PreparedClassifierPrimary) -> None:\n        from elspeth.web.sessions.continuation_classifier_primary import PreparedClassifierPrimary\n\n        with self._lock:\n            if self._state != "open" or self._prepared_candidate is not None or type(prepared) is not PreparedClassifierPrimary:\n                raise AuditIntegrityError("classifier preparation candidate is foreign or repeated")\n            self._prepared_candidate = prepared\n\n    def bind_prepared(self, prepared: PreparedClassifierPrimary) -> None:\n        from elspeth.web.sessions.continuation_classifier_primary import PreparedClassifierPrimary\n\n        with self._lock:\n            if (\n                self._state != "open"\n                or self._prepared is not None\n                or prepared is not self._prepared_candidate\n                or type(prepared) is not PreparedClassifierPrimary\n                or prepared.binding.running is not self.running\n                or prepared.binding.family != 0\n                or prepared.binding.input_sha256 != self._input_sha256\n                or prepared.snapshot.cursor != self._selected_cursor + 1\n                or prepared.snapshot.checkpoint_id == self._selected_checkpoint_id\n            ):\n                raise AuditIntegrityError("classifier no-SDK issuer changed its committed preparation")\n            self._prepared = prepared\n\n    def bind_custody(self, custody: ProviderCallCustody) -> None:\n        from elspeth.web.composer.provider_quota import ProviderCallCustody\n\n        with self._lock:\n            if (\n                self._state != "open"\n                or self._prepared is None\n                or self._custody is not None\n                or type(custody) is not ProviderCallCustody\n                or custody.context is not self.running.session_operation_context\n                or custody.continuation_primary_clock is not self.clock\n            ):\n                raise AuditIntegrityError("classifier no-SDK issuer cannot bind foreign physical custody")\n            self._custody = custody\n\n    def note_dispatched_candidate(self, dispatched: StoredContinuationSnapshot) -> None:\n        with self._lock:\n            if (\n                self._state != "open"\n                or self._prepared is None\n                or self._dispatched_candidate is not None\n                or type(dispatched) is not StoredContinuationSnapshot\n                or dispatched.cursor != self._prepared.snapshot.cursor + 1\n            ):\n                raise AuditIntegrityError("classifier dispatch candidate is foreign or repeated")\n            self._dispatched_candidate = dispatched\n\n    def bind_dispatched(self, dispatched: StoredContinuationSnapshot) -> None:\n        with self._lock:\n            if (\n                self._state != "open"\n                or self._prepared is None\n                or self._dispatched is not None\n                or dispatched is not self._dispatched_candidate\n                or dispatched.checkpoint_id == self._prepared.snapshot.checkpoint_id\n            ):\n                raise AuditIntegrityError("classifier no-SDK issuer changed its committed dispatch")\n            self._dispatched = dispatched\n\n    def note_admitted_attempt(self, custody: ProviderCallCustody, attempt: ProviderAttempt, *, model: str) -> None:\n        with self._lock:\n            if (\n                self._state != "open"\n                or custody is not self._custody\n                or type(attempt) is not ProviderAttempt\n                or type(model) is not str\n                or not model\n                or self._attempt is not None\n            ):\n                raise AuditIntegrityError("classifier no-SDK issuer lost its exact admitted attempt")\n            self._attempt = attempt\n            self._attempt_model = model\n\n    def note_sdk_entry(self, custody: ProviderCallCustody) -> None:\n        with self._lock:\n            if self._state != "open" or custody is not self._custody or self._dispatched is None or self._attempt is None:\n                raise AuditIntegrityError("classifier SDK entry conflicts with no-SDK disposition")\n            self._state = "sdk_entered"\n\n    def reserve_v4_refusal(\n        self, *, admission: V4ClassifierProviderAdmission, reason: ClassifierNoSdkRefusalReason, original_failure: BaseException\n    ) -> ClassifierNoSdkRefusalPermit:\n        from elspeth.web.composer.continuation_codec import decode_continuation_snapshot\n        from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy\n        from elspeth.web.sessions.continuation_classifier_primary import V4ClassifierProviderAdmission\n\n        with self._lock:\n            if self._prepared is None or self._custody is None:\n                raise AuditIntegrityError("classifier no-SDK refusal has no original source or custody")\n            root = decode_continuation_snapshot(self._prepared.snapshot.payload_json)\n            if (\n                type(decode_continuation_policy(root.admission_policy_json, root.policy_hash)) is not ContinuationAdmissionPolicyV4\n                or type(admission) is not V4ClassifierProviderAdmission\n                or self._state != "open"\n                or self._dispatched is not admission.dispatched\n                or self._attempt is not admission.attempt\n                or self._attempt_model != admission.no_sdk_tail.requested_model\n                or self._prepared.binding.effect_key != admission.effect_key\n                or self._permit is not None\n                or type(reason) is not ClassifierNoSdkRefusalReason\n                or (\n                    reason in (ClassifierNoSdkRefusalReason.PREPARATION_EXPIRED, ClassifierNoSdkRefusalReason.PRE_SDK_DEADLINE)\n                    and type(original_failure) is not TimeoutError\n                )\n                or (reason is ClassifierNoSdkRefusalReason.CHARGEABLE_REFUSED and type(original_failure) is not ChargeableAdmissionRefused)\n                or (reason is ClassifierNoSdkRefusalReason.COST_REFUSED and type(original_failure) is not CommonProviderCostRefused)\n                or (reason is ClassifierNoSdkRefusalReason.STORAGE_FAILURE and not isinstance(original_failure, SQLAlchemyError))\n                or (reason is ClassifierNoSdkRefusalReason.INTEGRITY_REFUSED and not isinstance(original_failure, AuditIntegrityError))\n                or (reason is ClassifierNoSdkRefusalReason.CANCELLED_BEFORE_SDK and type(original_failure) is not asyncio.CancelledError)\n                or (\n                    reason is ClassifierNoSdkRefusalReason.RETAINED_TAIL_UNBOUNDED\n                    and type(original_failure) is not ContinuationRetainedTailUnbounded\n                )\n            ):\n                raise AuditIntegrityError("classifier no-SDK refusal lacks proved unentered admitted custody")\n            self._custody.require_v4_classifier_unentered(admission)\n            permit = ClassifierNoSdkRefusalPermit(self, self._prepared, admission, reason, original_failure, str(uuid4()))\n            self._permit = permit\n            self._state = "refusing"\n            return permit\n\n    def require_refusal_permit(self, permit: ClassifierNoSdkRefusalPermit) -> None:\n        with self._lock:\n            if (\n                self._state not in ("refusing", "committed")\n                or type(permit) is not ClassifierNoSdkRefusalPermit\n                or permit is not self._permit\n            ):\n                raise AuditIntegrityError("classifier no-SDK permit is foreign, reused or entered")\n\n    def mark_refusal_committed(self, permit: ClassifierNoSdkRefusalPermit) -> None:\n        with self._lock:\n            self.require_refusal_permit(permit)\n            self._state = "committed"\n\n    def mark_unknown(self) -> None:\n        with self._lock:\n            if self._state == "open":\n                self._state = "unknown"',
    "src/elspeth/web/composer/continuation_classifier_no_sdk.py:ClassifierNoSdkRefusalPermit": "@dataclass(frozen=True, slots=True)\nclass ClassifierNoSdkRefusalPermit:\n    issuer: ClassifierNoSdkIssuer\n    prepared: PreparedClassifierPrimary\n    admission: V4ClassifierProviderAdmission\n    reason: ClassifierNoSdkRefusalReason\n    original_failure: BaseException\n    receipt_key: str",
    "src/elspeth/web/composer/continuation_title_no_sdk.py:TitleNoSdkIssuer": 'class TitleNoSdkIssuer:\n    """One original TITLE source, physical admission and observed failure.\n\n    Missing SQL audit rows do not establish this proof. The original gateway\n    must bind custody and its newly admitted attempt before SDK entry. The\n    original caller records the actual raised exception before requesting the\n    one permit. SQL still revalidates the current source and attempt under lock.\n    """\n\n    def __init__(self, prepared: PreparedTitleProvider) -> None:\n        from elspeth.web.sessions.continuation_title_provider import PreparedTitleProvider\n\n        if type(prepared) is not PreparedTitleProvider or prepared.work.running is not prepared.running:\n            raise AuditIntegrityError("title no-SDK issuer requires its original prepared operation")\n        self.prepared = prepared\n        self._lock = RLock()\n        self._custody: ProviderCallCustody | None = None\n        self._admission: V4TitleProviderAdmission | None = None\n        self._observed_model: str | None = None\n        self._failure: BaseException | None = None\n        self._permit: TitleNoSdkRefusalPermit | None = None\n        self._sdk_entered = False\n\n    def bind_custody(self, custody: ProviderCallCustody) -> None:\n        from elspeth.web.composer.provider_quota import ProviderCallCustody\n\n        with self._lock:\n            if (\n                type(custody) is not ProviderCallCustody\n                or self._custody is not None\n                or custody.continuation_title is not self.prepared\n                or custody.context is not self.prepared.running.session_operation_context\n                or custody.required_work.role is not RequiredWorkRole.TITLE\n                or custody.required_work.coordinator is not self.prepared.work.coordinator\n                or custody.required_work.transition_ordinal != self.prepared.work.transition_ordinal\n                or custody.required_work.semantic_ordinal != 6\n                or custody.attempt is not None\n                or custody.call is not None\n            ):\n                raise AuditIntegrityError("title no-SDK issuer changed its original unadmitted custody")\n            self._custody = custody\n\n    def bind_admission(self, custody: ProviderCallCustody, admission: V4TitleProviderAdmission, *, observed_model: str) -> None:\n        from elspeth.web.sessions.continuation_title_provider import V4TitleProviderAdmission\n\n        with self._lock:\n            if (\n                custody is not self._custody\n                or self._admission is not None\n                or type(admission) is not V4TitleProviderAdmission\n                or admission.tail.admitted != admission.selected\n                or observed_model != self.prepared.request.kwargs["model"]\n                or type(observed_model) is not str\n                or not observed_model\n            ):\n                raise AuditIntegrityError("title no-SDK issuer changed its original TITLE source or model")\n            custody.require_v4_title_unentered(admission)\n            self._admission, self._observed_model = admission, observed_model\n\n    def note_sdk_entry(self, custody: ProviderCallCustody) -> None:\n        with self._lock:\n            if custody is not self._custody or self._sdk_entered or self._permit is not None:\n                raise AuditIntegrityError("TITLE no-SDK issuer changed its original SDK entry")\n            self._sdk_entered = True\n\n    def note_original_failure(self, custody: ProviderCallCustody, error: BaseException) -> None:\n        with self._lock:\n            if custody is not self._custody or self._failure is not None or self._permit is not None:\n                raise AuditIntegrityError("title no-SDK issuer replaced its original failure")\n            self._failure = error\n\n    def reserve_refusal(self, *, reason: TitleNoSdkRefusalReason, original_failure: BaseException) -> TitleNoSdkRefusalPermit:\n        with self._lock:\n            expected = {\n                TitleNoSdkRefusalReason.GUARD_REFUSED: PromptGuardRefused,\n                TitleNoSdkRefusalReason.COST_REFUSED: CommonProviderCostRefused,\n                TitleNoSdkRefusalReason.CHARGEABLE_REFUSED: ChargeableAdmissionRefused,\n                TitleNoSdkRefusalReason.PRE_SDK_DEADLINE: TimeoutError,\n                TitleNoSdkRefusalReason.CANCELLED_BEFORE_SDK: asyncio.CancelledError,\n            }\n            if (\n                type(reason) is not TitleNoSdkRefusalReason\n                or type(original_failure) is not expected[reason]\n                or original_failure is not self._failure\n                or self._custody is None\n                or self._admission is None\n                or self._observed_model is None\n                or self._permit is not None\n                or self._sdk_entered\n            ):\n                raise AuditIntegrityError("title no-SDK refusal lost its original unentered failure")\n            self._custody.require_v4_title_unentered(self._admission)\n            permit = TitleNoSdkRefusalPermit(\n                self, self.prepared, self._admission, reason, original_failure, self._observed_model, str(uuid4())\n            )\n            self._permit = permit\n            return permit\n\n    def require_refusal_permit(self, permit: TitleNoSdkRefusalPermit) -> None:\n        with self._lock:\n            if (\n                type(permit) is not TitleNoSdkRefusalPermit\n                or permit is not self._permit\n                or permit.issuer is not self\n                or permit.prepared is not self.prepared\n                or permit.admission is not self._admission\n                or permit.original_failure is not self._failure\n                or permit.observed_model != self._observed_model\n                or self._custody is None\n                or self._admission is None\n                or self._sdk_entered\n            ):\n                raise AuditIntegrityError("title no-SDK refusal permit is foreign or copied")\n            self._custody.require_v4_title_unentered(self._admission)',
    "src/elspeth/web/composer/continuation_title_no_sdk.py:TitleNoSdkRefusalPermit": "@dataclass(frozen=True, slots=True)\nclass TitleNoSdkRefusalPermit:\n    issuer: TitleNoSdkIssuer\n    prepared: PreparedTitleProvider\n    admission: V4TitleProviderAdmission\n    reason: TitleNoSdkRefusalReason\n    original_failure: BaseException\n    observed_model: str\n    receipt_key: str",
    "src/elspeth/web/composer/provider_quota.py:ProviderCallCustody.require_v4_primary_unentered": 'def require_v4_primary_unentered(self, admission: V4PrimaryProviderAdmission) -> None:\n    from elspeth.web.sessions.continuation_primary import V4PrimaryProviderAdmission\n\n    if (\n        type(admission) is not V4PrimaryProviderAdmission\n        or self.continuation_primary_v4_admission is not admission\n        or self.attempt is not admission.attempt\n        or self._sdk_entered\n    ):\n        raise AuditIntegrityError("V4 PRIMARY no-SDK disposition lost its exact unentered physical hold")',
    "src/elspeth/web/composer/provider_quota.py:ProviderCallCustody.require_v4_classifier_unentered": 'def require_v4_classifier_unentered(self, admission: V4ClassifierProviderAdmission) -> None:\n    from elspeth.web.sessions.continuation_classifier_primary import V4ClassifierProviderAdmission\n\n    if (\n        type(admission) is not V4ClassifierProviderAdmission\n        or self.continuation_classifier_v4_admission is not admission\n        or self.attempt is not admission.attempt\n        or self._sdk_entered\n    ):\n        raise AuditIntegrityError("V4 classifier no-SDK disposition lost its exact unentered physical hold")',
    "src/elspeth/web/composer/provider_quota.py:ProviderCallCustody.require_v4_title_unentered": 'def require_v4_title_unentered(self, admission: V4TitleProviderAdmission) -> None:\n    from elspeth.web.sessions.continuation_title_provider import V4TitleProviderAdmission\n\n    prepared = self.continuation_title\n    if (\n        type(admission) is not V4TitleProviderAdmission\n        or prepared is None\n        or self.continuation_title_v4_admission is not admission\n        or self.continuation_title_selected is not admission.selected\n        or self.attempt is not admission.attempt\n        or self._sdk_entered\n        or self._v4_sdk_return is not None\n        or self.call is not None\n        or prepared.running.session_operation_context is not self.context\n    ):\n        raise AuditIntegrityError("V4 TITLE no-SDK disposition lost its exact unentered physical hold")',
    "src/elspeth/web/composer/provider_quota.py:ProviderCallCustody.note_v4_primary_no_sdk": 'def note_v4_primary_no_sdk(self, receipt: PrimaryNoSdkRefusalReceipt, admission: V4PrimaryProviderAdmission) -> None:\n    from elspeth.web.sessions.continuation_primary import PrimaryNoSdkRefusalReceipt\n\n    self.require_v4_primary_unentered(admission)\n    if (\n        type(receipt) is not PrimaryNoSdkRefusalReceipt\n        or receipt.effect_key != admission.hold.effect_key\n        or receipt.terminal.status != "failed"\n        or (receipt.terminal.checkpoint_id, receipt.terminal.journal_cursor) != (receipt.refused.checkpoint_id, receipt.refused.cursor)\n        or self.continuation_primary_v4_no_sdk_result is not None\n        or self.continuation_primary_v4_result is not None\n    ):\n        raise AuditIntegrityError("V4 PRIMARY no-SDK receipt lost its exact failed terminal")\n    self.continuation_primary_v4_no_sdk_result = receipt\n    self.attempt = None\n    self.call = None',
    "src/elspeth/web/composer/provider_quota.py:ProviderCallCustody.note_v4_classifier_no_sdk": 'def note_v4_classifier_no_sdk(self, receipt: ClassifierNoSdkRefusalReceipt, admission: V4ClassifierProviderAdmission) -> None:\n    from elspeth.web.sessions.continuation_classifier_primary_no_sdk import ClassifierNoSdkRefusalReceipt\n\n    self.require_v4_classifier_unentered(admission)\n    if (\n        type(receipt) is not ClassifierNoSdkRefusalReceipt\n        or receipt.effect_key != admission.effect_key\n        or receipt.terminal.status != "failed"\n        or (receipt.terminal.checkpoint_id, receipt.terminal.journal_cursor) != (receipt.refused.checkpoint_id, receipt.refused.cursor)\n        or self.continuation_classifier_v4_no_sdk_result is not None\n        or self.continuation_classifier_v4_result is not None\n    ):\n        raise AuditIntegrityError("V4 classifier no-SDK receipt lost its exact failed terminal")\n    self.continuation_classifier_v4_no_sdk_result = receipt\n    self.attempt = None\n    self.call = None',
    "src/elspeth/web/sessions/service.py:SessionServiceImpl.cancel_v4_continuation_primary_unentered": 'async def cancel_v4_continuation_primary_unentered(\n    self,\n    *,\n    prepared: PreparedContinuationProvider,\n    admission: V4PrimaryProviderAdmission,\n    permit: PrimaryNoSdkRefusalPermit,\n    required_work: RequiredWorkTicket,\n) -> RequiredSQLFinishOnce[PrimaryNoSdkRefusalReceipt]:\n    from elspeth.web.composer.continuation_primary_no_sdk import PrimaryNoSdkRefusalPermit\n    from elspeth.web.sessions.continuation import PreparedContinuationProvider\n    from elspeth.web.sessions.continuation_primary import V4PrimaryProviderAdmission, refuse_v4_primary_unentered_on_connection\n\n    if (\n        type(prepared) is not PreparedContinuationProvider\n        or type(admission) is not V4PrimaryProviderAdmission\n        or type(permit) is not PrimaryNoSdkRefusalPermit\n        or permit.prepared is not prepared\n        or permit.dispatched != admission.selected\n        or permit.undispatched_attempt_id != admission.attempt.attempt_id\n    ):\n        _refuse_required_sql(required_work, AuditIntegrityError("V4 PRIMARY zero-use refusal requires its original admitted source"))\n    context = prepared.binding.running.session_operation_context\n    _validate_required_sql_ticket(\n        required_work,\n        sources=(RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL,),\n        session_id=context.fence.session_id,\n        context=context,\n    )\n    sid = context.fence.session_id\n\n    def _sync() -> PrimaryNoSdkRefusalReceipt:\n        with (\n            self._session_process_locked_begin(sid) as conn,\n            self._session_write_lock(conn, sid),\n            self._session_composer_mutation_transaction(\n                conn,\n                session_id=sid,\n                session_operation_context=context,\n                expected_kind=SessionOperationKind.COMPOSE,\n                audit_only=True,\n            ),\n        ):\n\n            def _append_audit_event(event_id: str, content: str, created_at: datetime) -> None:\n                self._assert_session_write_lock_held(conn, sid, caller="cancel_v4_continuation_primary_unentered")\n                sequence_no = self._reserve_sequence_range(conn, sid, count=1)\n                self._insert_chat_message(\n                    conn,\n                    session_id=sid,\n                    role="audit",\n                    content=content,\n                    raw_content=None,\n                    tool_calls=None,\n                    sequence_no=sequence_no,\n                    writer_principal="compose_loop",\n                    composition_state_id=None,\n                    tool_call_id=None,\n                    parent_assistant_id=None,\n                    created_at=created_at,\n                    session_operation_context=context,\n                    message_id=event_id,\n                    audit_only=True,\n                )\n                with self._session_mutations(conn, session_id=sid, session_operation_context=context) as mutations:\n                    mutations.mark_session_updated(updated_at=created_at)\n\n            return refuse_v4_primary_unentered_on_connection(\n                conn, prepared=prepared, admission=admission, permit=permit, append_audit_event=_append_audit_event\n            )\n\n    return await run_required_sql_finish_once(required_work, _sync)',
    "src/elspeth/web/sessions/service.py:SessionServiceImpl.cancel_v4_continuation_title_unentered": 'async def cancel_v4_continuation_title_unentered(\n    self, *, permit: TitleNoSdkRefusalPermit, required_work: RequiredWorkTicket\n) -> RequiredSQLFinishOnce[VerifiedTitleNoSdkTerminal]:\n    from elspeth.web.composer.continuation_title_no_sdk import TitleNoSdkRefusalPermit\n    from elspeth.web.sessions.continuation_title_no_sdk_close import refuse_v4_title_no_sdk_on_connection\n    from elspeth.web.sessions.continuation_title_no_sdk_terminal import (\n        VerifiedTitleNoSdkTerminal,\n        read_v4_title_no_sdk_terminal_on_connection,\n    )\n    from elspeth.web.sessions.locking import locked_session_transaction\n\n    if type(permit) is not TitleNoSdkRefusalPermit:\n        _refuse_required_sql(required_work, AuditIntegrityError("V4 TITLE zero-use refusal requires its original permit"))\n    try:\n        permit.issuer.require_refusal_permit(permit)\n    except AuditIntegrityError as error:\n        _refuse_required_sql(required_work, error)\n    prepared = permit.prepared\n    context = prepared.running.session_operation_context\n    _validate_required_sql_ticket(\n        required_work,\n        sources=(RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL,),\n        session_id=context.fence.session_id,\n        context=context,\n    )\n    sid = context.fence.session_id\n\n    def _sync() -> VerifiedTitleNoSdkTerminal:\n        with (\n            self._session_process_locked_begin(sid) as conn,\n            self._session_write_lock(conn, sid),\n            self._session_composer_mutation_transaction(\n                conn,\n                session_id=sid,\n                session_operation_context=context,\n                expected_kind=SessionOperationKind.COMPOSE,\n                audit_only=True,\n            ),\n        ):\n            written = refuse_v4_title_no_sdk_on_connection(conn, service=self, permit=permit)\n        with locked_session_transaction(self._engine, sid) as conn:\n            cold = read_v4_title_no_sdk_terminal_on_connection(\n                conn,\n                session_id=prepared.running.claim.session_id,\n                operation_id=prepared.running.claim.operation_id,\n                names=prepared.names,\n            )\n        if (\n            type(cold) is not VerifiedTitleNoSdkTerminal\n            or written.status != "failed"\n            or (written.checkpoint_id, written.journal_cursor) != (cold.terminal.checkpoint_id, cold.terminal.cursor)\n            or cold.source != permit.admission.hold\n            or cold.provider_attempt_id != permit.admission.attempt.attempt_id\n            or cold.hold_state != "consumed"\n            or cold.reason is not permit.reason\n        ):\n            raise AuditIntegrityError("V4 TITLE zero-use refusal lost its committed cold terminal")\n        return cold\n\n    return await run_required_sql_finish_once(required_work, _sync)',
    "src/elspeth/web/sessions/service.py:SessionServiceImpl.cancel_v4_continuation_classifier_unentered": 'async def cancel_v4_continuation_classifier_unentered(\n    self,\n    *,\n    permit: ClassifierNoSdkRefusalPermit,\n    required_work: RequiredWorkTicket,\n) -> RequiredSQLFinishOnce[ClassifierNoSdkRefusalReceipt]:\n    from elspeth.web.composer.continuation_classifier_no_sdk import ClassifierNoSdkRefusalPermit\n    from elspeth.web.sessions.continuation_classifier_primary_no_sdk import refuse_classifier_no_sdk_on_connection\n\n    if type(permit) is not ClassifierNoSdkRefusalPermit:\n        _refuse_required_sql(required_work, AuditIntegrityError("V4 classifier zero-use refusal requires its original permit"))\n    permit.issuer.require_refusal_permit(permit)\n    context = permit.issuer.running.session_operation_context\n    _validate_required_sql_ticket(\n        required_work,\n        sources=(RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL,),\n        session_id=context.fence.session_id,\n        context=context,\n    )\n    sid = context.fence.session_id\n\n    def _sync() -> ClassifierNoSdkRefusalReceipt:\n        with (\n            self._session_process_locked_begin(sid) as conn,\n            self._session_write_lock(conn, sid),\n            self._session_composer_mutation_transaction(\n                conn,\n                session_id=sid,\n                session_operation_context=context,\n                expected_kind=SessionOperationKind.COMPOSE,\n                audit_only=True,\n            ),\n        ):\n\n            def _append_audit_event(event_id: str, content: str, created_at: datetime) -> None:\n                self._assert_session_write_lock_held(conn, sid, caller="cancel_v4_continuation_classifier_unentered")\n                sequence_no = self._reserve_sequence_range(conn, sid, count=1)\n                self._insert_chat_message(\n                    conn,\n                    session_id=sid,\n                    role="audit",\n                    content=content,\n                    raw_content=None,\n                    tool_calls=None,\n                    sequence_no=sequence_no,\n                    writer_principal="compose_loop",\n                    composition_state_id=None,\n                    tool_call_id=None,\n                    parent_assistant_id=None,\n                    created_at=created_at,\n                    session_operation_context=context,\n                    message_id=event_id,\n                    audit_only=True,\n                )\n                with self._session_mutations(conn, session_id=sid, session_operation_context=context) as mutations:\n                    mutations.mark_session_updated(updated_at=created_at)\n\n            return refuse_classifier_no_sdk_on_connection(conn, permit, append_audit_event=_append_audit_event)\n\n    return await run_required_sql_finish_once(required_work, _sync)',
    "src/elspeth/web/required_work.py:RequiredWorkCoordinator.validate_undispatched_cancellation_sql_outcome": 'def validate_undispatched_cancellation_sql_outcome[T](\n    self, *, ticket: RequiredWorkTicket, actual_outcome: RequiredSQLFinishOnce[T]\n) -> None:\n    """Verify the original zero-use cancellation SQL and its exact carrier."""\n    with self._lock:\n        self._registered(ticket, RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL)\n        with ticket._lock:\n            if ticket._finish_once_handoff is not actual_outcome:\n                raise AuditIntegrityError("Undispatched cancellation replaced actual finish-once carrier")\n            self._verify_sql_outcome(ticket, actual_outcome)',
    "src/elspeth/web/coordination/composer_operation_authority.py:ComposerAsyncOperationAuthority.read_continuation_primary_no_sdk_refusal": 'def read_continuation_primary_no_sdk_refusal(self, permit: PrimaryNoSdkRefusalPermit) -> PrimaryNoSdkRefusalReceipt | None:\n    from elspeth.web.composer.continuation_primary_no_sdk import PrimaryNoSdkRefusalPermit\n    from elspeth.web.sessions.continuation_primary import (\n        read_primary_no_sdk_refusal_on_connection,\n        selected_primary_effect_data_on_connection,\n    )\n\n    if type(permit) is not PrimaryNoSdkRefusalPermit:\n        raise AuditIntegrityError("PRIMARY readback requires its actual live no-SDK permit")\n    permit.issuer.require_refusal_permit(permit)\n    running = permit.issuer.running\n    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:\n        receipt = read_primary_no_sdk_refusal_on_connection(\n            conn,\n            session_id=running.claim.session_id,\n            operation_id=running.claim.operation_id,\n            effect_key=permit.prepared.binding.effect_key,\n        )\n        if receipt is not None:\n            if receipt.receipt_key != permit.receipt_key:\n                raise AuditIntegrityError("PRIMARY no-SDK readback selected another refusal")\n        else:\n            source = permit.dispatched if permit.dispatched is not None else permit.prepared.snapshot\n            selected_primary_effect_data_on_connection(\n                conn,\n                session_id=running.claim.session_id,\n                operation_id=running.claim.operation_id,\n                checkpoint_id=source.checkpoint_id,\n                cursor=source.cursor,\n                effect_key=permit.prepared.binding.effect_key,\n                input_sha256=permit.prepared.binding.input_sha256,\n            )\n    if receipt is not None:\n        permit.issuer.mark_refusal_committed(permit)\n    return receipt',
    "src/elspeth/web/sessions/continuation_primary.py:refuse_v4_primary_unentered_on_connection": "def refuse_v4_primary_unentered_on_connection(\n    conn: Connection,\n    *,\n    prepared: PreparedContinuationProvider,\n    admission: V4PrimaryProviderAdmission,\n    permit: PrimaryNoSdkRefusalPermit,\n    append_audit_event: Callable[[str, str, datetime], None],\n) -> PrimaryNoSdkRefusalReceipt:\n    from elspeth.web.sessions.continuation_primary_paid_overflow_refusal import refuse_v4_primary_unentered_on_connection as refuse\n\n    return refuse(\n        conn,\n        prepared=prepared,\n        admission=admission,\n        permit=permit,\n        append_audit_event=append_audit_event,\n    )",
    "src/elspeth/web/sessions/continuation_primary.py:read_primary_no_sdk_refusal_on_connection": 'def read_primary_no_sdk_refusal_on_connection(\n    conn: Connection, *, session_id: UUID, operation_id: str, effect_key: str\n) -> PrimaryNoSdkRefusalReceipt | None:\n    """Cold terminal proof; a missing receipt never implies an unentered SDK."""\n    from elspeth.web.coordination.composer_operation_authority import _record_from_row\n\n    sid = str(session_id)\n    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one_or_none()\n    if parent is None or parent.status != "failed":\n        return None\n    terminal = _record_from_row(parent)\n    stored = require_snapshot_cas(\n        conn,\n        session_id=sid,\n        operation_id=operation_id,\n        expected_checkpoint_id=terminal.checkpoint_id,\n        expected_cursor=terminal.journal_cursor,\n    )\n    root = decode_continuation_snapshot(stored.payload_json)\n    frame = _primary_frame(root)\n    if frame.provider_effect_key != effect_key or frame.provider_input_sha256 is None:\n        return None\n    _selected, _root, _frame, effect = selected_primary_effect_data_on_connection(\n        conn,\n        session_id=session_id,\n        operation_id=operation_id,\n        checkpoint_id=stored.checkpoint_id,\n        cursor=stored.cursor,\n        effect_key=effect_key,\n        input_sha256=frame.provider_input_sha256,\n    )\n    if effect.disposition != "refused":\n        return None\n    if (\n        type(decode_continuation_policy(root.admission_policy_json, root.policy_hash)) is ContinuationAdmissionPolicyV4\n        and effect.provider_attempt_id is not None\n    ):\n        from elspeth.web.sessions.continuation_primary_paid_overflow_refusal_terminal import read_v4_primary_no_sdk_refusal_on_connection\n\n        return read_v4_primary_no_sdk_refusal_on_connection(conn, session_id=session_id, operation_id=operation_id, effect_key=effect_key)\n    chunk_row = conn.execute(\n        select(chunks.c.codec).where(\n            chunks.c.session_id == sid,\n            chunks.c.operation_id == operation_id,\n            chunks.c.chunk_key == effect.result_chunk_key,\n        )\n    ).one_or_none()\n    if chunk_row is None or chunk_row.codec != "composer.primary-no-sdk-refusal.v1":\n        raise AuditIntegrityError("PRIMARY no-SDK receipt has no exact closed result chunk")\n    try:\n        canonical = read_chunk(conn, session_id=sid, operation_id=operation_id, key=effect.result_chunk_key).decode(\n            "utf-8", errors="strict"\n        )\n        chunk = encode_private_chunk(codec=chunk_row.codec, canonical=canonical)\n        data = _PrimaryNoSdkRefusalData.model_validate_json(canonical, strict=True)\n        failure = ComposerOperationError.model_validate_json(data.public_error_json, strict=True)\n    except (ValueError, ValidationError) as error:\n        raise AuditIntegrityError("PRIMARY no-SDK receipt violates its closed result codec") from error\n    if (\n        private_chunk_key(chunk) != effect.result_chunk_key\n        or canonical_json(data.model_dump(mode="json", exclude_unset=True)) != canonical\n        or canonical_json(failure.model_dump(mode="json")) != data.public_error_json\n        or terminal.result_json != data.public_error_json\n        or terminal.failure_code != failure.failure_code\n        or terminal.status != "failed"\n        or terminal.settled_by != "owner_terminal"\n        or (data.session_id, data.operation_id, data.effect_key, data.input_sha256) != (sid, operation_id, effect_key, effect.input_sha256)\n        or (data.creator_sequence, data.creator_generation) != (effect.creator_sequence, effect.creator_generation)\n        or (data.claim_attempt, data.operation_epoch) != (terminal.attempt, terminal.session_operation_epoch)\n        or data.receipt_key != effect.receipt_key\n        or (data.undispatched_attempt_id is None) != (data.undispatched_model is None)\n        or terminal.journal_cursor != data.source_cursor + 2\n    ):\n        raise AuditIntegrityError("PRIMARY no-SDK receipt contradicts its terminal/effect identity")\n    reason = PrimaryNoSdkRefusalReason(data.reason)\n    if reason in (\n        PrimaryNoSdkRefusalReason.GUARD_UNSUPPORTED_INPUT,\n        PrimaryNoSdkRefusalReason.GUARD_BLOCKED,\n        PrimaryNoSdkRefusalReason.GUARD_UNAVAILABLE,\n    ):\n        audit_id = data.guard_audit_id\n        if type(audit_id) is not str:\n            raise AuditIntegrityError("PRIMARY guard refusal terminal lost its audit locator")\n        terminal_operation_id = terminal.session_operation_id\n        terminal_operation_epoch = terminal.session_operation_epoch\n        if type(terminal_operation_id) is not str or type(terminal_operation_epoch) is not int:\n            raise AuditIntegrityError("PRIMARY guard refusal terminal lost its fenced operation")\n        if terminal.cancel_requested_at is not None:\n            from elspeth.web.sessions.composer_operation_errors import request_cancelled_error\n\n            if failure != request_cancelled_error(request_id=terminal.request_id):\n                raise AuditIntegrityError("PRIMARY concurrent Stop changed its selected public result")\n        decision = require_primary_guard_refusal_audit_on_connection(\n            conn,\n            session_id=sid,\n            operation_id=terminal_operation_id,\n            operation_epoch=terminal_operation_epoch,\n            reason=reason,\n            audit_id=audit_id,\n        )\n        if terminal.cancel_requested_at is None:\n            from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error\n\n            projected = project_composer_operation_error(\n                PromptGuardRefused(decision=decision, audit_id=audit_id), request_id=terminal.request_id\n            )\n            expected = projected.model_copy(update={"body": {"detail": projected.body}})\n            if failure != expected:\n                raise AuditIntegrityError("PRIMARY guard refusal terminal changed its public result")\n    elif data.guard_audit_id is not None:\n        raise AuditIntegrityError("PRIMARY non-guard refusal invented guard evidence")\n    source_row = conn.execute(\n        select(checkpoints).where(\n            checkpoints.c.session_id == sid,\n            checkpoints.c.operation_id == operation_id,\n            checkpoints.c.checkpoint_id == data.source_checkpoint_id,\n            checkpoints.c.cursor == data.source_cursor,\n        )\n    ).one_or_none()\n    refusal_row = conn.execute(\n        select(checkpoints).where(\n            checkpoints.c.session_id == sid,\n            checkpoints.c.operation_id == operation_id,\n            checkpoints.c.cursor == data.source_cursor + 1,\n        )\n    ).one_or_none()\n    refusal_event = conn.execute(\n        select(events).where(\n            events.c.session_id == sid,\n            events.c.operation_id == operation_id,\n            events.c.sequence == data.source_cursor + 1,\n        )\n    ).one_or_none()\n    settled_event = conn.execute(\n        select(events).where(\n            events.c.session_id == sid,\n            events.c.operation_id == operation_id,\n            events.c.sequence == terminal.journal_cursor,\n        )\n    ).one_or_none()\n    if source_row is None or refusal_row is None or refusal_event is None or settled_event is None:\n        raise AuditIntegrityError("PRIMARY no-SDK receipt lost its exact source/refusal/terminal journal")\n    try:\n        source_stack = decode_continuation_snapshot(\n            read_chunk(conn, session_id=sid, operation_id=operation_id, key=source_row.snapshot_chunk_key).decode("utf-8", errors="strict")\n        )\n        refused_stack = decode_continuation_snapshot(\n            read_chunk(conn, session_id=sid, operation_id=operation_id, key=refusal_row.snapshot_chunk_key).decode("utf-8", errors="strict")\n        )\n    except UnicodeDecodeError as error:\n        raise AuditIntegrityError("PRIMARY no-SDK journal lost strict snapshot bytes") from error\n    source_frontier = tuple(item for item in source_stack.effect_frontier if item.effect_id == effect_key)\n    expected_root = source_stack.model_copy(update={"effect_frontier": root.effect_frontier, "cursor": root.cursor})\n    if effect.action_json is not None:\n        from elspeth.web.composer.continuation_progress import (\n            ActionExecutionReceipt,\n            continuation_action_key,\n            decode_continuation_frontier,\n            decode_prescribed_continuation_action,\n            encode_continuation_frontier,\n            record_action_receipt,\n        )\n\n        if source_stack.semantic_frontier is None:\n            raise AuditIntegrityError("PRIMARY no-SDK source lost its original semantic frontier")\n        semantic_ref = source_stack.semantic_frontier\n        semantic_canonical = read_chunk(\n            conn, session_id=sid, operation_id=operation_id, key=f"value:{semantic_ref.codec}:{semantic_ref.digest}"\n        ).decode("utf-8", errors="strict")\n        verify_private_chunk(semantic_ref, semantic_canonical)\n        original_frontier = decode_continuation_frontier(semantic_canonical)\n        action = decode_prescribed_continuation_action(effect.action_json)\n        action_key = continuation_action_key(action, retained_evidence={item.receipt_id: item for item in original_frontier.evidence})\n        updated_frontier = record_action_receipt(\n            original_frontier,\n            ActionExecutionReceipt(action_key, effect_key, "refused"),\n            actual_owner_receipt_id=effect_key,\n        )\n        expected_ref = encode_private_chunk(\n            codec="composer.semantic-frontier.v1", canonical=encode_continuation_frontier(updated_frontier)\n        ).reference\n        expected_root = expected_root.model_copy(\n            update={"semantic_frontier": expected_ref, "active_action_json": None, "active_action_source": None}\n        )\n    if (\n        source_row.kind != "transition"\n        or refusal_row.kind != "transition"\n        or source_stack.cursor != data.source_cursor\n        or len(source_frontier) != 1\n        or (source_frontier[0].disposition, source_frontier[0].receipt_id) != (data.source_disposition, None)\n        or type(source_stack.frames[-1]) is not ComposeFrame\n        or source_stack.frames[-1].provider_effect_key != effect_key\n        or source_stack.frames[-1].provider_input_sha256 != effect.input_sha256\n        or refused_stack.cursor != data.source_cursor + 1\n        or refused_stack.effect_frontier != root.effect_frontier\n        or refused_stack.semantic_frontier != root.semantic_frontier\n        or refused_stack.active_action_json != root.active_action_json\n        or refused_stack.active_action_source != root.active_action_source\n        or expected_root != root\n        or refusal_event.tag != "effect_refused"\n        or read_chunk(conn, session_id=sid, operation_id=operation_id, key=refusal_event.payload_chunk_key)\n        != _primary_no_sdk_event(data, effect.result_chunk_key).encode("utf-8")\n        or settled_event.tag != "settled"\n        or read_chunk(conn, session_id=sid, operation_id=operation_id, key=settled_event.payload_chunk_key)\n        != canonical_json(\n            {\n                "version": 1,\n                "sequence": data.creator_sequence,\n                "generation": data.creator_generation,\n                "status": terminal.status,\n                "failure_code": terminal.failure_code,\n            }\n        ).encode("utf-8")\n    ):\n        raise AuditIntegrityError("PRIMARY no-SDK journal does not prove one refused source")\n    stored_primary_input_data_on_connection(conn, session_id=session_id, operation_id=operation_id, effect=effect, frame=frame)\n    if data.undispatched_attempt_id is not None:\n        attempt = conn.execute(select(physical).where(physical.c.attempt_id == data.undispatched_attempt_id)).one_or_none()\n        owner = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == sid)).scalar_one_or_none()\n        entry_id = _usage_entry_id(session_id=sid, source="composer", run_id=None, call_id=data.undispatched_attempt_id)\n        ledger = conn.execute(select(usage).where(usage.c.entry_id == entry_id)).one_or_none()\n        audit = conn.execute(\n            select(messages).where(messages.c.id == _undispatched_cancellation_event_id(data.undispatched_attempt_id))\n        ).one_or_none()\n        if (\n            attempt is None\n            or attempt.session_id != sid\n            or attempt.source != "composer"\n            or attempt.run_id is not None\n            or attempt.operation_id != terminal.session_operation_id\n            or attempt.operation_epoch != terminal.session_operation_epoch\n            or attempt.identity_id != owner\n            or attempt.settled_at is None\n            or attempt.ledger_entry_id != entry_id\n            or ledger is None\n            or ledger.identity_id != attempt.identity_id\n            or ledger.session_id != sid\n            or ledger.source != "composer"\n            or ledger.run_id is not None\n            or ledger.model != data.undispatched_model\n            or any(\n                value != 0\n                for value in (\n                    ledger.prompt_tokens,\n                    ledger.completion_tokens,\n                    ledger.cached_prompt_tokens,\n                    ledger.reasoning_tokens,\n                )\n            )\n            or audit is None\n            or audit.session_id != sid\n            or audit.role != "audit"\n            or audit.writer_principal != "compose_loop"\n            or audit.raw_content is not None\n            or audit.tool_calls is not None\n            or audit.composition_state_id is not None\n            or audit.tool_call_id is not None\n            or audit.parent_assistant_id is not None\n            or restore_utc(audit.created_at) != restore_utc(ledger.recorded_at)\n            or restore_utc(audit.created_at) != restore_utc(attempt.settled_at)\n        ):\n            raise AuditIntegrityError("PRIMARY no-SDK admitted attempt lacks its exact zero-use cancellation cohort")\n        if audit.content != canonical_json(\n            {\n                "kind": "provider_attempt.cancelled_before_dispatch",\n                "attempt_id": data.undispatched_attempt_id,\n                "source": "composer",\n                "run_id": None,\n                "requested_model": data.undispatched_model,\n                "operation_id": terminal.session_operation_id,\n                "operation_epoch": terminal.session_operation_epoch,\n                "lease_token_sha256": sha256(attempt.lease_token.encode("utf-8")).hexdigest(),\n            }\n        ):\n            raise AuditIntegrityError("PRIMARY no-SDK admitted attempt changed its cancellation audit")\n    return PrimaryNoSdkRefusalReceipt(\n        terminal,\n        StoredContinuationSnapshot(encode_continuation_snapshot(refused_stack), refusal_row.cursor, refusal_row.checkpoint_id),\n        effect_key,\n        effect.receipt_key,\n        effect.result_chunk_key,\n    )',
    "src/elspeth/web/sessions/continuation_primary_paid_overflow_refusal.py:refuse_v4_primary_unentered_on_connection": 'def refuse_v4_primary_unentered_on_connection(\n    conn: Connection,\n    *,\n    prepared: PreparedContinuationProvider,\n    admission: V4PrimaryProviderAdmission,\n    permit: PrimaryNoSdkRefusalPermit,\n    append_audit_event: Callable[[str, str, datetime], None],\n) -> PrimaryNoSdkRefusalReceipt:\n    """Cancel the original attempt and consume H with a bounded failed root."""\n    if (\n        type(prepared) is not PreparedContinuationProvider\n        or type(admission) is not V4PrimaryProviderAdmission\n        or type(permit) is not PrimaryNoSdkRefusalPermit\n        or permit.prepared is not prepared\n        or permit.dispatched != admission.selected\n        or permit.undispatched_attempt_id != admission.attempt.attempt_id\n        or permit.undispatched_model != admission.no_sdk_tail.requested_model\n    ):\n        raise AuditIntegrityError("V4 PRIMARY zero-use refusal changed its original admitted source")\n    permit.issuer.require_refusal_permit(permit)\n    running = prepared.binding.running\n    context = running.session_operation_context\n    selected, _root, frame, effect = _selected_effect(\n        conn,\n        running,\n        checkpoint_id=admission.selected.checkpoint_id,\n        cursor=admission.selected.cursor,\n        effect_key=prepared.binding.effect_key,\n        input_sha256=prepared.binding.input_sha256,\n    )\n    source = admission.hold\n    contract = _require_historical_hold_source(conn, source)\n    hold = _require_hold_fields(conn, source=source, contract=contract)\n    parent = conn.execute(select(jobs).where(jobs.c.session_id == source.session_id, jobs.c.operation_id == source.operation_id)).one()\n    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one()\n    if (\n        selected != admission.selected\n        or source.effect_key != effect.effect_key\n        or source.source_input_sha256 != effect.input_sha256\n        or effect.disposition != "dispatched"\n        or effect.provider_attempt_id != admission.attempt.attempt_id\n        or effect.receipt_key is not None\n        or effect.result_chunk_key is not None\n        or frame.phase != "awaiting_response"\n        or hold.state != "active"\n        or hold.provider_attempt_id != admission.attempt.attempt_id\n        or physical_row.settled_at is not None\n        or physical_row.ledger_entry_id is not None\n        or parent.status != "running"\n        or parent.current_segment_sequence != source.creator_sequence\n        or parent.ownership_generation != source.creator_generation\n        or parent.attempt != source.claim_attempt\n    ):\n        raise AuditIntegrityError("V4 PRIMARY zero-use refusal lost its pending physical effect and hold")\n    descriptor, _wire, _tools = stored_primary_input_data_on_connection(\n        conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, effect=effect, frame=frame\n    )\n    if descriptor.model != permit.undispatched_model:\n        raise AuditIntegrityError("V4 PRIMARY zero-use refusal changed its original requested model")\n    from elspeth.web.sessions.continuation_primary_paid_overflow_source import require_original_v4_primary_physical_source_on_connection\n\n    require_original_v4_primary_physical_source_on_connection(\n        conn, source=source, descriptor=descriptor, effect=effect, physical=physical_row, parent=parent, call=None\n    )\n    paid = prepare_primary_paid_overflow_tail_plan(\n        identity=admission.tail.identity, admitted=selected, creator_sequence=source.creator_sequence\n    )\n    if paid != admission.tail or paid.identity.attempt_id != admission.attempt.attempt_id:\n        raise AuditIntegrityError("V4 PRIMARY zero-use refusal changed its original paid tail")\n    refused_projection = _effect_snapshot(\n        selected.payload_json,\n        effect_key=source.effect_key,\n        disposition="refused",\n        receipt_id="00000000-0000-4000-8000-000000000000",\n    )\n    _refused_json, action_chunk = _v4_dispatch_action_projection(\n        conn,\n        root=decode_continuation_snapshot(selected.payload_json),\n        effect=effect,\n        dispatched_json=refused_projection,\n        disposition="refused",\n    )\n    no_sdk = prepare_primary_no_sdk_tail_plan(\n        admitted=selected,\n        attempt_id=admission.attempt.attempt_id,\n        requested_model=descriptor.model,\n        input_sha256=effect.input_sha256,\n        effect_key=effect.effect_key,\n        creator_sequence=source.creator_sequence,\n        creator_generation=source.creator_generation,\n        claim_attempt=source.claim_attempt,\n        operation_epoch=source.session_operation_epoch,\n        action_chunk=action_chunk,\n    )\n    incomplete_tail, reserved, combined_hash = primary_terminal_reservation(paid, no_sdk)\n    if (\n        no_sdk != admission.no_sdk_tail\n        or incomplete_tail != admission.incomplete_tail\n        or source.reserved_bytes != reserved\n        or source.fallback_contract_sha256 != combined_hash\n    ):\n        raise AuditIntegrityError("V4 PRIMARY zero-use refusal changed its combined original tail reserve")\n    segment = conn.execute(\n        select(segments).where(\n            segments.c.session_id == source.session_id,\n            segments.c.operation_id == source.operation_id,\n            segments.c.sequence == source.creator_sequence,\n        )\n    ).one()\n    history = conn.execute(\n        select(attempts).where(\n            attempts.c.session_id == source.session_id,\n            attempts.c.operation_id == source.operation_id,\n            attempts.c.sequence == source.creator_sequence,\n            attempts.c.generation == source.creator_generation,\n        )\n    ).one()\n    if segment.generation != source.creator_generation or segment.state != "running" or history.revoked_at is not None:\n        raise AuditIntegrityError("V4 PRIMARY zero-use refusal changed its original live segment")\n    now = database_now(conn)\n    batch = build_primary_no_sdk_tail_batch(\n        no_sdk,\n        permit=permit,\n        input_sha256=effect.input_sha256,\n        effect_key=effect.effect_key,\n        creator_sequence=source.creator_sequence,\n        creator_generation=source.creator_generation,\n        claim_attempt=source.claim_attempt,\n        operation_epoch=source.session_operation_epoch,\n        published_at=now,\n    )\n    result_chunks = batch.prospective[: 2 if action_chunk is not None else 1]\n    result_batch = V4PrimaryResultBatch(source, selected, result_chunks, batch.append, batch.prospective)\n    with admit_v4_primary_result_batch_on_connection(conn, batch=result_batch):\n        cancel_undispatched_provider_attempt_on_connection(\n            conn,\n            session_operation_context=context,\n            attempt_id=admission.attempt.attempt_id,\n            requested_model=descriptor.model,\n            append_audit_event=append_audit_event,\n        )\n        put_chunk(\n            conn,\n            session_id=source.session_id,\n            operation_id=source.operation_id,\n            key=batch.receipt_chunk_key,\n            payload=batch.receipt_json.encode("utf-8"),\n            codec="composer.primary-no-sdk-refusal.v1",\n        )\n        changed = conn.execute(\n            update(effects)\n            .where(\n                effects.c.session_id == source.session_id,\n                effects.c.operation_id == source.operation_id,\n                effects.c.effect_key == source.effect_key,\n                effects.c.disposition == "dispatched",\n                effects.c.provider_attempt_id == admission.attempt.attempt_id,\n                effects.c.receipt_key.is_(None),\n                effects.c.result_chunk_key.is_(None),\n            )\n            .values(disposition="refused", receipt_key=permit.receipt_key, result_chunk_key=batch.receipt_chunk_key)\n        )\n        if changed.rowcount != 1:\n            raise AuditIntegrityError("V4 PRIMARY zero-use refusal lost its original effect CAS")\n        if action_chunk is not None:\n            from elspeth.web.sessions.continuation import retain_action_execution_receipt_on_connection\n\n            actual_json = retain_action_execution_receipt_on_connection(\n                conn,\n                session_id=source.session_id,\n                operation_id=source.operation_id,\n                effect_key=source.effect_key,\n                disposition="refused",\n                snapshot_json=_effect_snapshot(\n                    selected.payload_json,\n                    effect_key=source.effect_key,\n                    disposition="refused",\n                    receipt_id=permit.receipt_key,\n                ),\n            )\n            if actual_json != batch.append.snapshot_json and decode_continuation_snapshot(actual_json).model_copy(\n                update={"cursor": batch.append.expected_cursor + 2}\n            ) != decode_continuation_snapshot(batch.append.snapshot_json):\n                raise AuditIntegrityError("V4 PRIMARY zero-use refusal changed its prescribed action bytes")\n        stored = append_complete_snapshot(\n            conn,\n            session_id=source.session_id,\n            operation_id=source.operation_id,\n            expected_checkpoint_id=selected.checkpoint_id,\n            expected_cursor=selected.cursor,\n            snapshot_json=batch.append.snapshot_json,\n            event_batch=batch.append.events,\n            handoff=False,\n            prepared_append=batch.append,\n        )\n        error = fixed_v4_primary_no_sdk_error(permit.reason)\n        _finalize_terminal_rows_after_append(\n            conn,\n            session_id=source.session_id,\n            operation_id=source.operation_id,\n            parent=parent,\n            segment=segment,\n            history=history,\n            retained=stored,\n            terminal_values=_terminal_values(error, now=now, settled_by="owner_terminal"),\n            attempt_outcome="settled",\n            now=now,\n        )\n        changed_hold = conn.execute(\n            update(holds)\n            .where(\n                holds.c.session_id == source.session_id,\n                holds.c.operation_id == source.operation_id,\n                holds.c.effect_key == source.effect_key,\n                holds.c.state == "active",\n                holds.c.provider_attempt_id == admission.attempt.attempt_id,\n            )\n            .values(state="consumed", consumed_at=database_now(conn))\n        )\n        if changed_hold.rowcount != 1:\n            raise AuditIntegrityError("V4 PRIMARY zero-use refusal lost its original active H")\n        record_primary_hold_consumed(conn, session_id=source.session_id, operation_id=source.operation_id, source=source)\n        terminal = _record_from_row(\n            conn.execute(select(jobs).where(jobs.c.session_id == source.session_id, jobs.c.operation_id == source.operation_id)).one()\n        )\n        if terminal.status != "failed" or terminal.journal_cursor != stored.cursor or terminal.checkpoint_id != stored.checkpoint_id:\n            raise AuditIntegrityError("V4 PRIMARY zero-use refusal lost its exact failed terminal")\n        return PrimaryNoSdkRefusalReceipt(terminal, stored, source.effect_key, permit.receipt_key, batch.receipt_chunk_key)',
    "src/elspeth/web/sessions/continuation_primary_paid_overflow_refusal_terminal.py:read_v4_primary_no_sdk_refusal_on_connection": "def read_v4_primary_no_sdk_refusal_on_connection(conn: Connection, *, session_id: UUID, operation_id: str, effect_key: str) -> PrimaryNoSdkRefusalReceipt | None:\n    sid = str(session_id)\n    source = _stored_hold_source(conn, session_id=sid, operation_id=operation_id, effect_key=effect_key)\n    if source is None:\n        return None\n    contract = _require_historical_hold_source(conn, source)\n    hold = _require_hold_fields(conn, source=source, contract=contract)\n    effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == effect_key)).one_or_none()\n    if effect is None or effect.disposition != 'refused':\n        return None\n    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one()\n    terminal = load_snapshot(conn, session_id=sid, operation_id=operation_id)\n    terminal_root = decode_continuation_snapshot(terminal.payload_json)\n    frame = _primary_frame(terminal_root)\n    terminal_row = conn.execute(select(checkpoints).where(checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation_id, checkpoints.c.checkpoint_id == terminal.checkpoint_id)).one()\n    admitted_row = conn.execute(select(checkpoints).where(checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation_id, checkpoints.c.cursor == source.source_cursor + 1)).one_or_none()\n    if admitted_row is None or admitted_row.kind != 'transition' or terminal_row.kind != 'transition':\n        raise AuditIntegrityError('V4 PRIMARY zero-use refusal lost its original dispatch and terminal heads')\n    admitted_text = _bounded_bytes(conn, source, admitted_row.snapshot_chunk_key, 'snapshot.v2').decode('utf-8', errors='strict')\n    admitted = StoredContinuationSnapshot(admitted_text, admitted_row.cursor, admitted_row.checkpoint_id)\n    admitted_root = decode_continuation_snapshot(admitted_text)\n    admitted_frame = _primary_frame(admitted_root)\n    admitted_effects = tuple((entry for entry in admitted_root.effect_frontier if entry.effect_id == effect_key))\n    terminal_effects = tuple((entry for entry in terminal_root.effect_frontier if entry.effect_id == effect_key))\n    if terminal_root.logical != admitted_root.logical or terminal_root.policy_hash != source.policy_sha256 or frame.provider_effect_key != effect_key or (frame.provider_input_sha256 != source.source_input_sha256) or (effect.provider_attempt_id != hold.provider_attempt_id) or (type(effect.provider_attempt_id) is not str) or (type(effect.receipt_key) is not str) or (type(effect.result_chunk_key) is not str) or (effect.response_finished_at is not None) or (parent.status != 'failed') or (parent.settled_by != 'owner_terminal') or (parent.journal_cursor != terminal.cursor) or (parent.checkpoint_id != terminal.checkpoint_id) or (hold.state != 'consumed') or (terminal.cursor != admitted.cursor + 2) or (admitted_frame.provider_effect_key != effect_key) or (admitted_frame.provider_input_sha256 != source.source_input_sha256) or (admitted_frame.phase != 'awaiting_response') or (len(admitted_effects) != 1) or ((admitted_effects[0].effect_id, admitted_effects[0].disposition, admitted_effects[0].receipt_id) != (effect_key, 'dispatched', None)) or (len(terminal_effects) != 1) or ((terminal_effects[0].effect_id, terminal_effects[0].disposition, terminal_effects[0].receipt_id) != (effect_key, 'refused', effect.receipt_key)) or (_bounded_bytes(conn, source, terminal_row.snapshot_chunk_key, 'snapshot.v2') != terminal.payload_json.encode('utf-8')):\n        raise AuditIntegrityError('V4 PRIMARY zero-use refusal changed its original terminal/effect/H')\n    try:\n        receipt_text = _bounded_bytes(conn, source, effect.result_chunk_key, 'composer.primary-no-sdk-refusal.v1').decode('utf-8', errors='strict')\n        data = _PrimaryNoSdkRefusalData.model_validate_json(receipt_text, strict=True)\n        reason = PrimaryNoSdkRefusalReason(data.reason)\n    except (UnicodeDecodeError, ValueError, ValidationError) as error:\n        raise AuditIntegrityError('V4 PRIMARY zero-use refusal lost its closed receipt') from error\n    if canonical_json(data.model_dump(mode='json', exclude_unset=True)) != receipt_text or data.source_disposition != 'dispatched' or (data.session_id, data.operation_id, data.effect_key, data.input_sha256) != (sid, operation_id, effect_key, effect.input_sha256) or ((data.creator_sequence, data.creator_generation) != (source.creator_sequence, source.creator_generation)) or ((data.claim_attempt, data.operation_epoch) != (source.claim_attempt, source.session_operation_epoch)) or ((data.source_checkpoint_id, data.source_cursor) != (admitted.checkpoint_id, admitted.cursor)) or (data.receipt_key != effect.receipt_key) or (data.undispatched_attempt_id != effect.provider_attempt_id) or (data.undispatched_model != effect.requested_model) or (data.public_error_json != canonical_json(fixed_v4_primary_no_sdk_error(reason).model_dump(mode='json'))) or (private_chunk_key(encode_private_chunk(codec='composer.primary-no-sdk-refusal.v1', canonical=receipt_text)) != effect.result_chunk_key) or (parent.result_json != data.public_error_json) or (type(parent.result_json) is not str) or (parent.result_sha256 != composer_operation_result_hash(parent.result_json)) or (parent.failure_code != 'http_error'):\n        raise AuditIntegrityError('V4 PRIMARY zero-use receipt contradicts its actual failed terminal')\n    descriptor, _wire, _tools = stored_primary_input_data_on_connection(conn, session_id=session_id, operation_id=operation_id, effect=effect, frame=frame)\n    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == effect.provider_attempt_id)).one_or_none()\n    if physical_row is None or descriptor.model != data.undispatched_model or physical_row.settled_at is None:\n        raise AuditIntegrityError('V4 PRIMARY zero-use refusal lost its original physical request')\n    require_original_v4_primary_physical_source_on_connection(conn, source=source, descriptor=descriptor, effect=effect, physical=physical_row, parent=parent, call=None)\n    paid = prepare_primary_paid_overflow_tail_plan(identity=source_paid_identity(source, effect.provider_attempt_id), admitted=admitted, creator_sequence=source.creator_sequence)\n    refused_projection = _effect_snapshot_for_reader(admitted.payload_json, effect_key, data.receipt_key)\n    _projected, action_chunk = _v4_dispatch_action_projection(conn, root=admitted_root, effect=effect, dispatched_json=refused_projection, disposition='refused')\n    no_sdk = prepare_primary_no_sdk_tail_plan(admitted=admitted, attempt_id=effect.provider_attempt_id, requested_model=descriptor.model, input_sha256=effect.input_sha256, effect_key=effect_key, creator_sequence=source.creator_sequence, creator_generation=source.creator_generation, claim_attempt=source.claim_attempt, operation_epoch=source.session_operation_epoch, action_chunk=action_chunk)\n    _incomplete_tail, reserved, combined_hash = primary_terminal_reservation(paid, no_sdk, historical_contract_sha256=source.fallback_contract_sha256)\n    projected, projected_events = _shape(admitted=admitted, receipt_key=data.receipt_key, receipt_chunk_key=effect.result_chunk_key, input_sha256=effect.input_sha256, effect_key=effect_key, creator_sequence=source.creator_sequence, creator_generation=source.creator_generation, action_chunk=action_chunk)\n    expected_terminal = encode_continuation_snapshot(decode_continuation_snapshot(projected).model_copy(update={'cursor': terminal.cursor}))\n    if source.reserved_bytes != reserved or source.fallback_contract_sha256 != combined_hash or len(receipt_text.encode('utf-8')) > no_sdk.maximum_receipt_bytes or (expected_terminal != terminal.payload_json):\n        raise AuditIntegrityError('V4 PRIMARY zero-use refusal changed its original bounded tail')\n    for index, expected in enumerate(projected_events, start=1):\n        event = conn.execute(select(events).where(events.c.session_id == sid, events.c.operation_id == operation_id, events.c.sequence == admitted.cursor + index)).one_or_none()\n        if event is None or event.tag != expected.tag or _bounded_bytes(conn, source, event.payload_chunk_key, 'event.v2') != expected.payload_json.encode('utf-8'):\n            raise AuditIntegrityError('V4 PRIMARY zero-use refusal lost its exact two-event journal')\n    dispatch = conn.execute(select(events).where(events.c.session_id == sid, events.c.operation_id == operation_id, events.c.sequence == admitted.cursor)).one_or_none()\n    if dispatch is None or dispatch.tag != 'may_dispatch' or _bounded_bytes(conn, source, dispatch.payload_chunk_key, 'event.v2') != canonical_json({'effect_key': effect_key}).encode('utf-8'):\n        raise AuditIntegrityError('V4 PRIMARY zero-use refusal lost its original dispatch event')\n    entry_id = _usage_entry_id(session_id=sid, source='composer', run_id=None, call_id=effect.provider_attempt_id)\n    ledger = conn.execute(select(usage).where(usage.c.entry_id == entry_id)).one_or_none()\n    audit = conn.execute(select(messages).where(messages.c.id == _undispatched_cancellation_event_id(effect.provider_attempt_id))).one_or_none()\n    if ledger is None or audit is None or physical_row.ledger_entry_id != entry_id or (ledger.identity_id != physical_row.identity_id) or (ledger.model != descriptor.model) or (ledger.session_id != sid) or (ledger.source != 'composer') or (ledger.run_id is not None) or any((value != 0 for value in (ledger.prompt_tokens, ledger.completion_tokens, ledger.cached_prompt_tokens, ledger.reasoning_tokens))) or (audit.session_id != sid) or (audit.role != 'audit') or (audit.writer_principal != 'compose_loop') or (audit.tool_calls is not None) or (audit.raw_content is not None) or (audit.composition_state_id is not None) or (audit.tool_call_id is not None) or (audit.parent_assistant_id is not None) or (restore_utc(audit.created_at) != restore_utc(ledger.recorded_at)) or (restore_utc(audit.created_at) != restore_utc(physical_row.settled_at)) or (audit.content != canonical_json({'kind': 'provider_attempt.cancelled_before_dispatch', 'attempt_id': effect.provider_attempt_id, 'source': 'composer', 'run_id': None, 'requested_model': descriptor.model, 'operation_id': source.session_operation_id, 'operation_epoch': source.session_operation_epoch, 'lease_token_sha256': source.lease_sha256})):\n        raise AuditIntegrityError('V4 PRIMARY zero-use refusal lost its exact zero-token cancellation cohort')\n    return PrimaryNoSdkRefusalReceipt(_record_from_row(parent), terminal, effect_key, data.receipt_key, effect.result_chunk_key)",
    "src/elspeth/web/sessions/continuation_title_no_sdk_close.py:refuse_v4_title_no_sdk_on_connection": 'def refuse_v4_title_no_sdk_on_connection(\n    conn: Connection, *, service: SessionServiceImpl, permit: TitleNoSdkRefusalPermit\n) -> ComposerOperationRecord:\n    """Verify issuer, cancel original attempt, retain exact refusal and consume H."""\n    if type(permit) is not TitleNoSdkRefusalPermit:\n        raise AuditIntegrityError("TITLE no-SDK refusal requires its original nominal permit")\n    permit.issuer.require_refusal_permit(permit)\n    prepared = permit.prepared\n    if service is not prepared.composer._require_sessions_service():\n        raise AuditIntegrityError("TITLE no-SDK refusal requires its original sessions writer")\n    # Cancellation allocates and inserts the canonical audit message. Acquire\n    # the allocator\'s transaction-scoped session lock before touching any\n    # physical/ledger/terminal rows, and retain it for the complete closure.\n    with service._session_write_lock(conn, str(prepared.running.claim.session_id)):\n        return _refuse_v4_title_no_sdk_locked_on_connection(conn, service=service, permit=permit)',
    "src/elspeth/web/sessions/continuation_title_no_sdk_close.py:_refuse_v4_title_no_sdk_locked_on_connection": 'def _refuse_v4_title_no_sdk_locked_on_connection(\n    conn: Connection, *, service: SessionServiceImpl, permit: TitleNoSdkRefusalPermit\n) -> ComposerOperationRecord:\n    permit.issuer.require_refusal_permit(permit)\n    prepared = permit.prepared\n    admission = permit.admission\n    if (\n        type(prepared) is not PreparedTitleProvider\n        or type(admission) is not V4TitleProviderAdmission\n        or admission.tail.admitted != admission.selected\n        or permit.observed_model != prepared.request.kwargs["model"]\n        or admission.no_sdk_tail.requested_model != prepared.request.kwargs["model"]\n    ):\n        raise AuditIntegrityError("TITLE no-SDK refusal changed original phase/model/attempt")\n    running = prepared.running\n    selected = admission.selected\n    _current(conn, running, selected, owed=True)\n    descriptor, reference, stage, _origin = _source_chain(conn, running, selected, names=prepared.names)\n    if stage != "admitted" or reference != prepared.input.reference:\n        raise AuditIntegrityError("TITLE no-SDK refusal lost its original admitted input")\n    source = admission.hold\n    sid, operation = source.session_id, source.operation_id\n    child = conn.execute(\n        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == source.effect_key)\n    ).one()\n    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id).with_for_update()).one()\n    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()\n    segment = conn.execute(\n        select(segments).where(\n            segments.c.session_id == sid, segments.c.operation_id == operation, segments.c.sequence == source.creator_sequence\n        )\n    ).one()\n    history = conn.execute(\n        select(attempts).where(\n            attempts.c.session_id == sid,\n            attempts.c.operation_id == operation,\n            attempts.c.sequence == source.creator_sequence,\n            attempts.c.generation == source.creator_generation,\n        )\n    ).one()\n    if (\n        child.disposition != "dispatched"\n        or child.provider_attempt_id != admission.attempt.attempt_id\n        or child.requested_model != permit.observed_model\n        or child.receipt_key is not None\n        or child.result_chunk_key is not None\n        or child.response_finished_at is not None\n        or physical_row.settled_at is not None\n        or physical_row.ledger_entry_id is not None\n        or parent.status != "running"\n        or parent.current_segment_sequence != source.creator_sequence\n        or parent.ownership_generation != source.creator_generation\n        or parent.attempt != source.claim_attempt\n        or segment.state != "running"\n        or segment.generation != source.creator_generation\n        or history.claim_attempt != source.claim_attempt\n        or history.revoked_at is not None\n    ):\n        raise AuditIntegrityError("TITLE no-SDK refusal lacks one original unsettled physical owner")\n    from elspeth.web.sessions.continuation_title_paid_overflow_source import require_original_v4_title_physical_source_on_connection\n\n    requested_model = require_original_v4_title_physical_source_on_connection(\n        conn, source=source, descriptor=descriptor, effect=child, physical=physical_row, parent=parent\n    )\n    if requested_model != permit.observed_model:\n        raise AuditIntegrityError("TITLE no-SDK refusal changed its original physical requested model")\n    published_at = database_now(conn)\n    batch = build_title_no_sdk_tail_batch(\n        admission.no_sdk_tail,\n        reason=permit.reason,\n        receipt_key=permit.receipt_key,\n        published_at=published_at,\n    )\n    context = running.session_operation_context\n\n    def append_audit_event(event_id: str, content: str, created_at: datetime) -> None:\n        sequence = service._reserve_sequence_range(conn, sid, count=1)\n        service._insert_chat_message(\n            conn,\n            session_id=sid,\n            role="audit",\n            content=content,\n            raw_content=None,\n            tool_calls=None,\n            sequence_no=sequence,\n            writer_principal="compose_loop",\n            composition_state_id=None,\n            tool_call_id=None,\n            parent_assistant_id=None,\n            created_at=created_at,\n            session_operation_context=context,\n            message_id=event_id,\n            audit_only=True,\n        )\n        with service._session_mutations(conn, session_id=sid, session_operation_context=context) as mutations:\n            mutations.mark_session_updated(updated_at=created_at)\n\n    with admit_v4_title_no_sdk_tail_on_connection(\n        conn, source=source, paid_plan=admission.tail, incomplete_plan=admission.incomplete_tail, plan=admission.no_sdk_tail, batch=batch\n    ):\n        cancel_undispatched_provider_attempt_on_connection(\n            conn,\n            session_operation_context=context,\n            attempt_id=admission.attempt.attempt_id,\n            requested_model=permit.observed_model,\n            append_audit_event=append_audit_event,\n        )\n        put_chunk(\n            conn,\n            session_id=sid,\n            operation_id=operation,\n            key=batch.receipt_chunk_key,\n            payload=batch.receipt_json.encode("utf-8"),\n            codec="composer.title-no-sdk.v1",\n        )\n        changed = conn.execute(\n            update(effects)\n            .where(\n                effects.c.session_id == sid,\n                effects.c.operation_id == operation,\n                effects.c.effect_key == source.effect_key,\n                effects.c.disposition == "dispatched",\n                effects.c.provider_attempt_id == admission.attempt.attempt_id,\n                effects.c.receipt_key.is_(None),\n                effects.c.result_chunk_key.is_(None),\n            )\n            .values(disposition="refused", receipt_key=permit.receipt_key, result_chunk_key=batch.receipt_chunk_key)\n        )\n        if changed.rowcount != 1:\n            raise AuditIntegrityError("TITLE no-SDK refusal lost original dispatched child CAS")\n        retained = append_complete_snapshot(\n            conn,\n            session_id=sid,\n            operation_id=operation,\n            expected_checkpoint_id=selected.checkpoint_id,\n            expected_cursor=selected.cursor,\n            snapshot_json=batch.append.snapshot_json,\n            event_batch=batch.append.events,\n            handoff=False,\n            prepared_append=batch.append,\n        )\n        _finalize_terminal_rows_after_append(\n            conn,\n            session_id=sid,\n            operation_id=operation,\n            parent=parent,\n            segment=segment,\n            history=history,\n            retained=retained,\n            terminal_values=_terminal_values(fixed_title_no_sdk_error(permit.reason), now=published_at, settled_by="owner_terminal"),\n            attempt_outcome="settled",\n            now=published_at,\n        )\n        verified = read_v4_title_no_sdk_terminal_on_connection(\n            conn,\n            session_id=running.claim.session_id,\n            operation_id=operation,\n            names=prepared.names,\n        )\n        if (\n            verified is None\n            or verified.source != source\n            or verified.terminal != retained\n            or verified.receipt_json != batch.receipt_json\n            or verified.provider_attempt_id != admission.attempt.attempt_id\n            or verified.hold_state != "active"\n        ):\n            raise AuditIntegrityError("TITLE no-SDK refusal cannot consume an unverified original terminal")\n        contract = _require_historical_hold_source(conn, source)\n        hold = _require_hold_fields(conn, source=source, contract=contract)\n        if hold.state != "active" or hold.provider_attempt_id != admission.attempt.attempt_id:\n            raise AuditIntegrityError("TITLE no-SDK refusal lost original active H")\n        consumed = conn.execute(\n            update(holds)\n            .where(\n                holds.c.session_id == sid,\n                holds.c.operation_id == operation,\n                holds.c.effect_key == source.effect_key,\n                holds.c.state == "active",\n                holds.c.provider_attempt_id == admission.attempt.attempt_id,\n            )\n            .values(state="consumed", consumed_at=database_now(conn))\n        )\n        if consumed.rowcount != 1:\n            raise AuditIntegrityError("TITLE no-SDK refusal could not consume exact H")\n        record_paid_tail_hold_consumed(conn, session_id=sid, operation_id=operation, source=source)\n        post = read_v4_title_no_sdk_terminal_on_connection(\n            conn,\n            session_id=running.claim.session_id,\n            operation_id=operation,\n            names=prepared.names,\n        )\n        if (\n            post is None\n            or post.hold_state != "consumed"\n            or post.receipt_json != verified.receipt_json\n            or post.terminal != verified.terminal\n        ):\n            raise AuditIntegrityError("TITLE no-SDK refusal changed after H consumption")\n    final = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()\n    if final.status != "failed" or final.checkpoint_id != retained.checkpoint_id or final.journal_cursor != retained.cursor:\n        raise AuditIntegrityError("TITLE no-SDK refusal lost its atomic failed parent")\n    return _record_from_row(final)',
    "src/elspeth/web/sessions/continuation_title_no_sdk_terminal.py:read_v4_title_no_sdk_terminal_on_connection": "def read_v4_title_no_sdk_terminal_on_connection(conn: Connection, *, session_id: UUID, operation_id: str, names: frozenset[str]) -> VerifiedTitleNoSdkTerminal | None:\n    \"\"\"Return only an original zero-use failed terminal, never retry authority.\"\"\"\n    from elspeth.web.sessions.continuation_title_no_sdk_close import fixed_title_no_sdk_error\n    if type(session_id) is not UUID or type(operation_id) is not str or type(names) is not frozenset:\n        raise TypeError('TITLE no-SDK cold proof requires owned coordinates and current names')\n    sid = str(session_id)\n    source = _stored_hold_source(conn, session_id=sid, operation_id=operation_id)\n    if source is None:\n        return None\n    contract = _require_historical_hold_source(conn, source)\n    hold = _require_hold_fields(conn, source=source, contract=contract)\n    effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == source.effect_key)).one()\n    if effect.disposition != 'refused':\n        return None\n    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one()\n    terminal = load_snapshot(conn, session_id=sid, operation_id=operation_id)\n    terminal_row = conn.execute(select(checkpoints).where(checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation_id, checkpoints.c.checkpoint_id == terminal.checkpoint_id)).one()\n    admitted_row = conn.execute(select(checkpoints).where(checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation_id, checkpoints.c.cursor == source.source_cursor + 1)).one_or_none()\n    if admitted_row is None or admitted_row.kind != 'transition' or terminal_row.kind != 'transition':\n        raise AuditIntegrityError('TITLE no-SDK lost its original dispatch and terminal heads')\n    admitted_payload = _bounded_bytes(conn, source, admitted_row.snapshot_chunk_key, 'snapshot.v2').decode('utf-8', errors='strict')\n    admitted = StoredContinuationSnapshot(admitted_payload, admitted_row.cursor, admitted_row.checkpoint_id)\n    from elspeth.web.sessions.continuation_title_provider import title_source_chain_data_on_connection\n    descriptor, _ref, stage, _predecessor = title_source_chain_data_on_connection(conn, session_id=session_id, operation_id=operation_id, sequence=source.creator_sequence, generation=source.creator_generation, selected=admitted, names=names, terminal_failed_request=True)\n    if stage != 'admitted' or descriptor.input_sha256 != source.source_input_sha256:\n        raise AuditIntegrityError('TITLE no-SDK changed its original prepared/admitted input')\n    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == effect.provider_attempt_id)).one_or_none()\n    if effect.owner != 'provider' or effect.input_sha256 != source.source_input_sha256 or effect.requested_model is None or (effect.receipt_key is None) or (effect.result_chunk_key is None) or (effect.provider_attempt_id is None) or (hold.provider_attempt_id != effect.provider_attempt_id) or (physical_row is None) or (physical_row.session_id != sid) or (physical_row.source != 'auto_title') or (physical_row.run_id is not None) or (physical_row.operation_id != source.session_operation_id) or (physical_row.operation_epoch != source.session_operation_epoch) or (sha256(physical_row.lease_token.encode('utf-8')).hexdigest() != source.lease_sha256) or (physical_row.ledger_entry_id is None) or (physical_row.settled_at is None):\n        raise AuditIntegrityError('TITLE no-SDK changed its original effect/physical/hold binding')\n    require_original_v4_title_physical_source_on_connection(conn, source=source, descriptor=descriptor, effect=effect, physical=physical_row, parent=parent)\n    identity = PaidOverflowIdentity(session_id=sid, operation_id=operation_id, effect_key=effect.effect_key, attempt_id=effect.provider_attempt_id, session_operation_id=source.session_operation_id, session_operation_epoch=source.session_operation_epoch, ownership_generation=source.creator_generation, lease_sha256=source.lease_sha256, policy_sha256=source.policy_sha256, source_checkpoint_id=source.source_checkpoint_id, source_cursor=source.source_cursor, source_snapshot_sha256=source.source_snapshot_sha256)\n    from elspeth.web.sessions.continuation_title_no_sdk_tail import build_title_no_sdk_tail_batch, prepare_title_no_sdk_tail_plan\n    from elspeth.web.sessions.continuation_title_paid_incomplete_tail import prepare_title_paid_incomplete_tail_plan\n    paid_plan = prepare_title_paid_overflow_tail_plan(identity=identity, admitted=admitted, creator_sequence=source.creator_sequence)\n    incomplete_plan = prepare_title_paid_incomplete_tail_plan(identity=identity, admitted=admitted, creator_sequence=source.creator_sequence)\n    no_sdk_plan = prepare_title_no_sdk_tail_plan(identity=identity, admitted=admitted, creator_sequence=source.creator_sequence, requested_model=effect.requested_model)\n    from elspeth.web.sessions.continuation_title_terminal_reservation import title_sdk_terminal_reservation\n    reserved, combined_hash = title_sdk_terminal_reservation(paid_plan, incomplete_plan, no_sdk_plan, historical_contract_sha256=source.fallback_contract_sha256)\n    if source.reserved_bytes != reserved or source.fallback_contract_sha256 != combined_hash:\n        raise AuditIntegrityError('TITLE no-SDK terminal changed its original combined H')\n    receipt_json = _bounded_bytes(conn, source, effect.result_chunk_key, 'composer.title-no-sdk.v1').decode('utf-8', errors='strict')\n    receipt, error = parse_json_strict(receipt_json)\n    if error is not None or type(receipt) is not dict or receipt.get('reason') not in {item.value for item in TitleNoSdkRefusalReason} or (receipt_json != canonical_json(receipt)):\n        raise AuditIntegrityError('TITLE no-SDK terminal lost its closed refusal receipt')\n    reason = TitleNoSdkRefusalReason(receipt['reason'])\n    batch = build_title_no_sdk_tail_batch(no_sdk_plan, reason=reason, receipt_key=effect.receipt_key, published_at=restore_utc(terminal_row.created_at))\n    if receipt_json != batch.receipt_json or effect.receipt_key != batch.receipt_key or effect.result_chunk_key != batch.receipt_chunk_key or (effect.response_finished_at is not None) or (terminal.cursor != admitted.cursor + 2) or (terminal.payload_json != batch.append.snapshot_json) or (terminal_row.snapshot_chunk_key != f'snapshot:{terminal.checkpoint_id}') or (_bounded_bytes(conn, source, terminal_row.snapshot_chunk_key, 'snapshot.v2') != terminal.payload_json.encode('utf-8')) or (parent.status != 'failed') or (parent.settled_by != 'owner_terminal') or (parent.failure_code != 'http_error') or (parent.journal_cursor != terminal.cursor) or (parent.checkpoint_id != terminal.checkpoint_id) or (parent.result_schema != COMPOSER_OPERATION_RESULT_SCHEMA_ERROR) or (parent.result_json != canonical_json(fixed_title_no_sdk_error(reason).model_dump(mode='json'))) or (parent.result_sha256 != composer_operation_result_hash(parent.result_json)):\n        raise AuditIntegrityError('TITLE no-SDK terminal lost its exact failed public result')\n    successor_checkpoints = conn.execute(select(checkpoints.c.checkpoint_id).where(checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation_id, checkpoints.c.cursor > admitted.cursor)).all()\n    if tuple((row.checkpoint_id for row in successor_checkpoints)) != (terminal.checkpoint_id,):\n        raise AuditIntegrityError('TITLE no-SDK has another terminal snapshot outside its reserved batch')\n    event_rows = conn.execute(select(events).where(events.c.session_id == sid, events.c.operation_id == operation_id, events.c.sequence > admitted.cursor).order_by(events.c.sequence)).all()\n    if len(event_rows) != 2:\n        raise AuditIntegrityError('TITLE no-SDK has a changed terminal attribution length')\n    for row, expected in zip(event_rows, batch.append.events, strict=True):\n        if row.tag != expected.tag or row.payload_chunk_key != f'event:{row.sequence}' or _bounded_bytes(conn, source, row.payload_chunk_key, 'event.v2') != expected.payload_json.encode('utf-8'):\n            raise AuditIntegrityError('TITLE no-SDK changed its exact terminal attribution event')\n    entry_id = _usage_entry_id(session_id=sid, source='auto_title', run_id=None, call_id=effect.provider_attempt_id)\n    entry = conn.execute(select(ledger).where(ledger.c.entry_id == entry_id)).one_or_none()\n    audit_id = _undispatched_cancellation_event_id(effect.provider_attempt_id)\n    audit = conn.execute(select(messages).where(messages.c.id == audit_id)).one_or_none()\n    expected_content = canonical_json({'kind': 'provider_attempt.cancelled_before_dispatch', 'attempt_id': effect.provider_attempt_id, 'source': 'auto_title', 'run_id': None, 'requested_model': effect.requested_model, 'operation_id': source.session_operation_id, 'operation_epoch': source.session_operation_epoch, 'lease_token_sha256': source.lease_sha256})\n    if entry is None or audit is None or physical_row.ledger_entry_id != entry_id or (entry.session_id != sid) or (entry.identity_id != physical_row.identity_id) or (entry.source != 'auto_title') or (entry.run_id is not None) or (entry.model != effect.requested_model) or any((value != 0 for value in (entry.prompt_tokens, entry.completion_tokens, entry.cached_prompt_tokens, entry.reasoning_tokens))) or (audit.session_id != sid) or (audit.role != 'audit') or (audit.writer_principal != 'compose_loop') or (audit.content != expected_content) or (audit.raw_content is not None) or (audit.tool_calls is not None) or (audit.composition_state_id is not None) or (audit.tool_call_id is not None) or (audit.parent_assistant_id is not None) or (restore_utc(audit.created_at) != restore_utc(entry.recorded_at)) or (restore_utc(physical_row.settled_at) != restore_utc(entry.recorded_at)) or (restore_utc(entry.recorded_at) < restore_utc(physical_row.started_at)):\n        raise AuditIntegrityError('TITLE no-SDK terminal lost exact zero-use audit and ledger')\n    segment = conn.execute(select(segments).where(segments.c.session_id == sid, segments.c.operation_id == operation_id, segments.c.sequence == source.creator_sequence)).one()\n    history = conn.execute(select(attempts).where(attempts.c.session_id == sid, attempts.c.operation_id == operation_id, attempts.c.sequence == source.creator_sequence, attempts.c.generation == source.creator_generation)).one()\n    final_root = decode_continuation_snapshot(terminal.payload_json)\n    actual_effects = set(conn.execute(select(effects.c.effect_key, effects.c.disposition, effects.c.receipt_key).where(effects.c.session_id == sid, effects.c.operation_id == operation_id)).tuples())\n    if actual_effects != {(item.effect_id, item.disposition, item.receipt_id) for item in final_root.effect_frontier} or segment.state != 'settled' or segment.generation != source.creator_generation or (history.outcome != 'settled') or (history.revoked_at is None) or (parent.current_segment_sequence != source.creator_sequence) or (parent.ownership_generation != source.creator_generation) or final_root.pending_settlement_ids or final_root.pending_owner_results or (len(final_root.effect_frontier) != 1) or (final_root.effect_frontier[0].disposition != 'refused'):\n        raise AuditIntegrityError('TITLE no-SDK lost its closed segment and effect frontier')\n    return VerifiedTitleNoSdkTerminal(source, receipt_json, terminal, identity.attempt_id, hold.state, reason)",
    "src/elspeth/web/sessions/continuation_classifier_primary_no_sdk.py:refuse_classifier_no_sdk_on_connection": "def refuse_classifier_no_sdk_on_connection(conn: Connection, permit: ClassifierNoSdkRefusalPermit, *, append_audit_event: Callable[[str, str, datetime], None]) -> ClassifierNoSdkRefusalReceipt:\n    \"\"\"Atomically cancel the unentered attempt and close its effect, H and owner.\"\"\"\n    if type(permit) is not ClassifierNoSdkRefusalPermit:\n        raise AuditIntegrityError('classifier no-SDK writer requires its actual live permit')\n    permit.issuer.require_refusal_permit(permit)\n    prepared, admission = (permit.prepared, permit.admission)\n    running = prepared.binding.running\n    source = _read_source(conn, running.claim.session_id, running.claim.operation_id, admission.effect_key)\n    selected = _chain(conn, source, dispatched=True)\n    hold_source = admission.hold\n    contract = _require_historical_hold_source(conn, hold_source)\n    hold = _require_hold_fields(conn, source=hold_source, contract=contract)\n    parent = conn.execute(select(jobs).where(jobs.c.session_id == hold_source.session_id, jobs.c.operation_id == hold_source.operation_id)).one()\n    effect = conn.execute(select(effects).where(effects.c.session_id == hold_source.session_id, effects.c.operation_id == hold_source.operation_id, effects.c.effect_key == hold_source.effect_key)).one()\n    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one()\n    session_owner = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == hold_source.session_id)).scalar_one()\n    if selected != admission.dispatched or prepared.snapshot != _chain(conn, source, dispatched=False) or prepared.binding.running is not permit.issuer.running or (source.effect_key != prepared.binding.effect_key) or (source.input_sha256 != prepared.binding.input_sha256) or (hold_source.effect_key != source.effect_key) or (hold_source.source_input_sha256 != source.input_sha256) or (hold_source.policy_sha256 != decode_continuation_snapshot(selected.payload_json).policy_hash) or (hold_source.creator_sequence != source.creator_sequence) or (hold_source.creator_generation != source.creator_generation) or (hold_source.claim_attempt != running.claim.attempt) or (effect.disposition != 'dispatched') or (effect.provider_attempt_id != admission.attempt.attempt_id) or (effect.input_sha256 != source.input_sha256) or (effect.requested_model != admission.no_sdk_tail.requested_model) or (effect.owner != 'provider') or (effect.action_json is not None) or (effect.receipt_key is not None) or (effect.result_chunk_key is not None) or ((effect.creator_sequence, effect.creator_generation) != (source.creator_sequence, source.creator_generation)) or (hold.state != 'active') or (hold.provider_attempt_id != admission.attempt.attempt_id) or (physical_row.settled_at is not None) or (physical_row.ledger_entry_id is not None) or (physical_row.session_id != hold_source.session_id) or (physical_row.identity_id != session_owner) or (physical_row.source != 'composer') or (physical_row.operation_id != hold_source.session_operation_id) or (physical_row.operation_epoch != hold_source.session_operation_epoch) or (parent.status != 'running') or (parent.current_segment_sequence != source.creator_sequence) or (parent.ownership_generation != source.creator_generation) or (parent.attempt != running.claim.attempt):\n        raise AuditIntegrityError('classifier no-SDK writer lost its original physical effect, hold or owner')\n    paid = prepare_classifier_paid_overflow_tail_plan(identity=admission.paid_tail.identity, admitted=selected, creator_sequence=source.creator_sequence)\n    incomplete = prepare_classifier_paid_incomplete_tail_plan(identity=admission.incomplete_tail.identity, admitted=selected, creator_sequence=source.creator_sequence)\n    no_sdk = prepare_classifier_no_sdk_tail_plan(admitted=selected, attempt_id=admission.attempt.attempt_id, requested_model=effect.requested_model, input_sha256=source.input_sha256, effect_key=source.effect_key, creator_sequence=source.creator_sequence, creator_generation=source.creator_generation, claim_attempt=running.claim.attempt, operation_epoch=hold_source.session_operation_epoch)\n    reservation = ClassifierTerminalReservation(*classifier_sdk_terminal_reservation(paid, incomplete, no_sdk, historical_contract_sha256=hold_source.fallback_contract_sha256))\n    if paid != admission.paid_tail or incomplete != admission.incomplete_tail or no_sdk != admission.no_sdk_tail or (paid.identity.attempt_id != admission.attempt.attempt_id) or (hold_source.reserved_bytes != reservation.reserved_bytes) or (hold_source.fallback_contract_sha256 != reservation.contract_sha256):\n        raise AuditIntegrityError('classifier no-SDK writer changed its combined original tail reserve')\n    segment = conn.execute(select(segments).where(segments.c.session_id == hold_source.session_id, segments.c.operation_id == hold_source.operation_id, segments.c.sequence == source.creator_sequence)).one()\n    history = conn.execute(select(segment_attempts).where(segment_attempts.c.session_id == hold_source.session_id, segment_attempts.c.operation_id == hold_source.operation_id, segment_attempts.c.sequence == source.creator_sequence, segment_attempts.c.generation == source.creator_generation)).one()\n    if segment.generation != source.creator_generation or segment.state != 'running' or history.revoked_at is not None:\n        raise AuditIntegrityError('classifier no-SDK writer changed its live segment')\n    now = database_now(conn)\n    batch = build_classifier_no_sdk_tail_batch(no_sdk, permit=permit, input_sha256=source.input_sha256, effect_key=source.effect_key, creator_sequence=source.creator_sequence, creator_generation=source.creator_generation, claim_attempt=running.claim.attempt, operation_epoch=hold_source.session_operation_epoch, published_at=now)\n    result_batch = V4PrimaryResultBatch(hold_source, selected, batch.prospective[:1], batch.append, batch.prospective)\n    with admit_v4_primary_result_batch_on_connection(conn, batch=result_batch):\n        cancel_undispatched_provider_attempt_on_connection(conn, session_operation_context=running.session_operation_context, attempt_id=admission.attempt.attempt_id, requested_model=no_sdk.requested_model, append_audit_event=append_audit_event)\n        put_chunk(conn, session_id=hold_source.session_id, operation_id=hold_source.operation_id, key=batch.receipt_chunk_key, payload=batch.receipt_json.encode('utf-8'), codec=_CODEC)\n        changed = conn.execute(update(effects).where(effects.c.session_id == hold_source.session_id, effects.c.operation_id == hold_source.operation_id, effects.c.effect_key == hold_source.effect_key, effects.c.disposition == 'dispatched', effects.c.provider_attempt_id == admission.attempt.attempt_id, effects.c.receipt_key.is_(None), effects.c.result_chunk_key.is_(None)).values(disposition='refused', receipt_key=permit.receipt_key, result_chunk_key=batch.receipt_chunk_key))\n        if changed.rowcount != 1:\n            raise AuditIntegrityError('classifier no-SDK writer lost its original effect CAS')\n        stored = append_complete_snapshot(conn, session_id=hold_source.session_id, operation_id=hold_source.operation_id, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.cursor, snapshot_json=batch.append.snapshot_json, event_batch=batch.append.events, handoff=False, prepared_append=batch.append)\n        error = fixed_classifier_no_sdk_error(permit.reason)\n        _finalize_terminal_rows_after_append(conn, session_id=hold_source.session_id, operation_id=hold_source.operation_id, parent=parent, segment=segment, history=history, retained=stored, terminal_values=_terminal_values(error, now=now, settled_by='owner_terminal'), attempt_outcome='settled', now=now)\n        changed_hold = conn.execute(update(holds).where(holds.c.session_id == hold_source.session_id, holds.c.operation_id == hold_source.operation_id, holds.c.effect_key == hold_source.effect_key, holds.c.state == 'active', holds.c.provider_attempt_id == admission.attempt.attempt_id).values(state='consumed', consumed_at=database_now(conn)))\n        if changed_hold.rowcount != 1:\n            raise AuditIntegrityError('classifier no-SDK writer lost its original active H')\n        record_primary_hold_consumed(conn, session_id=hold_source.session_id, operation_id=hold_source.operation_id, source=hold_source)\n        terminal = _record_from_row(conn.execute(select(jobs).where(jobs.c.session_id == hold_source.session_id, jobs.c.operation_id == hold_source.operation_id)).one())\n        if terminal.status != 'failed' or terminal.journal_cursor != stored.cursor or terminal.checkpoint_id != stored.checkpoint_id:\n            raise AuditIntegrityError('classifier no-SDK writer lost its exact failed terminal')\n        return ClassifierNoSdkRefusalReceipt(terminal, stored, source.effect_key, permit.receipt_key, batch.receipt_chunk_key)",
    "src/elspeth/web/sessions/continuation_classifier_primary_no_sdk.py:read_classifier_no_sdk_refusal_on_connection": "def read_classifier_no_sdk_refusal_on_connection(conn: Connection, *, session_id: UUID, operation_id: str, effect_key: str) -> ClassifierNoSdkRefusalReceipt | None:\n    \"\"\"Cold proof of the exact two-frame refusal, zero-use cohort and consumed H.\"\"\"\n    if type(session_id) is not UUID or type(operation_id) is not str or type(effect_key) is not str:\n        raise TypeError('classifier no-SDK cold proof requires nominal SQL identity')\n    sid = str(session_id)\n    hold_source = _stored_hold_source(conn, session_id=sid, operation_id=operation_id, effect_key=effect_key)\n    if hold_source is None:\n        return None\n    contract = _require_historical_hold_source(conn, hold_source)\n    hold = _require_hold_fields(conn, source=hold_source, contract=contract)\n    effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == effect_key)).one_or_none()\n    if effect is None or effect.disposition != 'refused':\n        return None\n    source = _read_source(conn, session_id, operation_id, effect_key)\n    prepared = _chain(conn, source, dispatched=False)\n    if hold_source.source_checkpoint_id != prepared.checkpoint_id or hold_source.source_cursor != prepared.cursor or hold_source.source_snapshot_sha256 != sha256(prepared.payload_json.encode('utf-8')).hexdigest():\n        raise AuditIntegrityError('classifier no-SDK cold proof changed its original prepared H source')\n    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one()\n    terminal = load_snapshot(conn, session_id=sid, operation_id=operation_id)\n    terminal_row = conn.execute(select(checkpoints).where(checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation_id, checkpoints.c.checkpoint_id == terminal.checkpoint_id)).one()\n    admitted_row = conn.execute(select(checkpoints).where(checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation_id, checkpoints.c.cursor == hold_source.source_cursor + 1)).one_or_none()\n    if admitted_row is None or admitted_row.kind != 'transition' or terminal_row.kind != 'transition':\n        raise AuditIntegrityError('classifier no-SDK cold proof lost original dispatch and terminal heads')\n    admitted_text = _bounded_bytes(conn, hold_source, admitted_row.snapshot_chunk_key, 'snapshot.v2').decode('utf-8', errors='strict')\n    admitted = StoredContinuationSnapshot(admitted_text, admitted_row.cursor, admitted_row.checkpoint_id)\n    if _chain(conn, source, dispatched=True) != admitted:\n        raise AuditIntegrityError('classifier no-SDK cold proof changed its two-frame original dispatch')\n    admitted_root = decode_continuation_snapshot(admitted.payload_json)\n    terminal_root = decode_continuation_snapshot(terminal.payload_json)\n    applied_batch_cut(admitted_root.frames)\n    applied_batch_cut(terminal_root.frames)\n    if effect.provider_attempt_id != hold.provider_attempt_id or type(effect.provider_attempt_id) is not str or type(effect.receipt_key) is not str or (type(effect.result_chunk_key) is not str) or (effect.action_json is not None) or (effect.response_finished_at is not None) or (effect.input_sha256 != source.input_sha256) or (effect.requested_model is None) or (parent.status != 'failed') or (parent.settled_by != 'owner_terminal') or (parent.journal_cursor != terminal.cursor) or (parent.checkpoint_id != terminal.checkpoint_id) or (hold.state != 'consumed') or (terminal.cursor != admitted.cursor + 2) or (terminal_root.policy_hash != hold_source.policy_sha256) or (terminal_root.logical != admitted_root.logical) or (_bounded_bytes(conn, hold_source, terminal_row.snapshot_chunk_key, 'snapshot.v2') != terminal.payload_json.encode('utf-8')):\n        raise AuditIntegrityError('classifier no-SDK cold proof changed terminal, effect or H')\n    try:\n        receipt_text = _bounded_bytes(conn, hold_source, effect.result_chunk_key, _CODEC).decode('utf-8', errors='strict')\n        data = ClassifierNoSdkReceiptData.model_validate_json(receipt_text, strict=True)\n        reason = ClassifierNoSdkRefusalReason(data.reason)\n    except (UnicodeDecodeError, ValueError, ValidationError) as error:\n        raise AuditIntegrityError('classifier no-SDK cold proof lost its closed receipt') from error\n    no_sdk = prepare_classifier_no_sdk_tail_plan(admitted=admitted, attempt_id=effect.provider_attempt_id, requested_model=effect.requested_model, input_sha256=source.input_sha256, effect_key=effect_key, creator_sequence=source.creator_sequence, creator_generation=source.creator_generation, claim_attempt=hold_source.claim_attempt, operation_epoch=hold_source.session_operation_epoch)\n    from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity\n    fence_identity = PaidOverflowIdentity(session_id=sid, operation_id=operation_id, effect_key=effect_key, attempt_id=effect.provider_attempt_id, session_operation_id=hold_source.session_operation_id, session_operation_epoch=hold_source.session_operation_epoch, ownership_generation=hold_source.creator_generation, lease_sha256=hold_source.lease_sha256, policy_sha256=hold_source.policy_sha256, source_checkpoint_id=hold_source.source_checkpoint_id, source_cursor=hold_source.source_cursor, source_snapshot_sha256=hold_source.source_snapshot_sha256)\n    paid = prepare_classifier_paid_overflow_tail_plan(identity=fence_identity, admitted=admitted, creator_sequence=source.creator_sequence)\n    incomplete = prepare_classifier_paid_incomplete_tail_plan(identity=fence_identity, admitted=admitted, creator_sequence=source.creator_sequence)\n    reservation = ClassifierTerminalReservation(*classifier_sdk_terminal_reservation(paid, incomplete, no_sdk, historical_contract_sha256=hold_source.fallback_contract_sha256))\n    projected, projected_events = _shape(admitted, receipt_key=data.receipt_key, receipt_chunk_key=effect.result_chunk_key, input_sha256=source.input_sha256, effect_key=effect_key, creator_sequence=source.creator_sequence, creator_generation=source.creator_generation)\n    expected_terminal = encode_continuation_snapshot(decode_continuation_snapshot(projected).model_copy(update={'cursor': terminal.cursor}))\n    if canonical_json(data.model_dump(mode='json')) != receipt_text or (data.session_id, data.operation_id, data.effect_key, data.input_sha256) != (sid, operation_id, effect_key, source.input_sha256) or (data.creator_sequence, data.creator_generation) != (source.creator_sequence, source.creator_generation) or ((data.claim_attempt, data.operation_epoch) != (hold_source.claim_attempt, hold_source.session_operation_epoch)) or ((data.source_checkpoint_id, data.source_cursor) != (admitted.checkpoint_id, admitted.cursor)) or (data.receipt_key != effect.receipt_key) or (data.undispatched_attempt_id != effect.provider_attempt_id) or (data.undispatched_model != effect.requested_model) or (data.public_error_json != canonical_json(fixed_classifier_no_sdk_error(reason).model_dump(mode='json'))) or (private_chunk_key(encode_private_chunk(codec=_CODEC, canonical=receipt_text)) != effect.result_chunk_key) or (parent.result_json != data.public_error_json) or (type(parent.result_json) is not str) or (parent.result_sha256 != composer_operation_result_hash(parent.result_json)) or (parent.failure_code != 'http_error') or (hold_source.reserved_bytes != reservation.reserved_bytes) or (hold_source.fallback_contract_sha256 != reservation.contract_sha256) or (len(receipt_text.encode('utf-8')) > no_sdk.maximum_receipt_bytes) or (expected_terminal != terminal.payload_json):\n        raise AuditIntegrityError('classifier no-SDK cold receipt contradicts its bounded terminal')\n    for index, expected in enumerate(projected_events, start=1):\n        event = conn.execute(select(events).where(events.c.session_id == sid, events.c.operation_id == operation_id, events.c.sequence == admitted.cursor + index)).one_or_none()\n        if event is None or event.tag != expected.tag or _bounded_bytes(conn, hold_source, event.payload_chunk_key, 'event.v2') != expected.payload_json.encode('utf-8'):\n            raise AuditIntegrityError('classifier no-SDK cold proof lost its exact two-event journal')\n    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == effect.provider_attempt_id)).one_or_none()\n    session_owner = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == sid)).scalar_one_or_none()\n    entry_id = _usage_entry_id(session_id=sid, source='composer', run_id=None, call_id=effect.provider_attempt_id)\n    ledger = conn.execute(select(usage).where(usage.c.entry_id == entry_id)).one_or_none()\n    audit = conn.execute(select(messages).where(messages.c.id == _undispatched_cancellation_event_id(effect.provider_attempt_id))).one_or_none()\n    if physical_row is None or ledger is None or audit is None or (physical_row.session_id != sid) or (physical_row.identity_id != session_owner) or (physical_row.source != 'composer') or (physical_row.run_id is not None) or (physical_row.operation_id != hold_source.session_operation_id) or (physical_row.operation_epoch != hold_source.session_operation_epoch) or (physical_row.settled_at is None) or (physical_row.ledger_entry_id != entry_id) or (ledger.session_id != sid) or (ledger.source != 'composer') or (ledger.run_id is not None) or (ledger.identity_id != physical_row.identity_id) or (ledger.model != effect.requested_model) or any((value != 0 for value in (ledger.prompt_tokens, ledger.completion_tokens, ledger.cached_prompt_tokens, ledger.reasoning_tokens))) or (audit.session_id != sid) or (audit.role != 'audit') or (audit.writer_principal != 'compose_loop') or (audit.raw_content is not None) or (audit.tool_calls is not None) or (restore_utc(audit.created_at) != restore_utc(ledger.recorded_at)) or (restore_utc(audit.created_at) != restore_utc(physical_row.settled_at)) or (audit.content != canonical_json({'kind': 'provider_attempt.cancelled_before_dispatch', 'attempt_id': effect.provider_attempt_id, 'source': 'composer', 'run_id': None, 'requested_model': effect.requested_model, 'operation_id': hold_source.session_operation_id, 'operation_epoch': hold_source.session_operation_epoch, 'lease_token_sha256': sha256(physical_row.lease_token.encode('utf-8')).hexdigest()})):\n        raise AuditIntegrityError('classifier no-SDK cold proof lost its zero-use physical cancellation cohort')\n    appended_checkpoints = conn.execute(select(checkpoints.c.checkpoint_id).where(checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation_id, checkpoints.c.cursor > admitted.cursor, checkpoints.c.cursor <= terminal.cursor).order_by(checkpoints.c.cursor)).scalars().all()\n    if appended_checkpoints != [terminal.checkpoint_id]:\n        raise AuditIntegrityError('classifier no-SDK cold proof changed its single terminal append')\n    return ClassifierNoSdkRefusalReceipt(_record_from_row(parent), terminal, effect_key, effect.receipt_key, effect.result_chunk_key)",
}

V4_NO_SDK_IMPORTS = {
    "src/elspeth/web/composer/continuation_primary.py": [
        ["asyncio", "asyncio", None],
        ["time", "time", None],
        ["Callable", "collections.abc", "Callable"],
        ["replace", "dataclasses", "replace"],
        ["Any", "typing", "Any"],
        ["AuditIntegrityError", "elspeth.contracts.errors", "AuditIntegrityError"],
        ["canonical_json", "elspeth.contracts.hashing", "canonical_json"],
        ["run_required_sql_finish_once", "elspeth.web.async_workers", "run_required_sql_finish_once"],
        ["_CallModelOutcome", "elspeth.web.composer._compose_loop_carriers", "_CallModelOutcome"],
        ["BufferingRecorder", "elspeth.web.composer.audit", "BufferingRecorder"],
        ["ComposeFrame", "elspeth.web.composer.continuation", "ComposeFrame"],
        ["RecorderDrainCursors", "elspeth.web.composer.continuation_audit_buffer", "RecorderDrainCursors"],
        ["ContinuationActionClock", "elspeth.web.composer.continuation_clock", "ContinuationActionClock"],
        ["decode_turn_evidence", "elspeth.web.composer.continuation_evidence", "decode_turn_evidence"],
        ["PrimaryNoSdkIssuer", "elspeth.web.composer.continuation_primary_no_sdk", "PrimaryNoSdkIssuer"],
        ["PrimaryNoSdkRefusalReason", "elspeth.web.composer.continuation_primary_no_sdk", "PrimaryNoSdkRefusalReason"],
        [
            "observe_v4_primary_provider_result",
            "elspeth.web.composer.continuation_primary_observation",
            "observe_v4_primary_provider_result",
        ],
        ["SelectedContinuationState", "elspeth.web.composer.continuation_selection", "SelectedContinuationState"],
        ["_state_from_tool_json", "elspeth.web.composer.continuation_tool_batch", "_state_from_tool_json"],
        ["ProviderCallCustody", "elspeth.web.composer.provider_quota", "ProviderCallCustody"],
        ["ProviderInvocationFamily", "elspeth.web.composer.provider_quota", "ProviderInvocationFamily"],
        ["ProviderInvocationOwner", "elspeth.web.composer.provider_quota", "ProviderInvocationOwner"],
        ["V4PaidIncompleteCommitted", "elspeth.web.composer.provider_quota", "V4PaidIncompleteCommitted"],
        ["V4SdkTerminalCommitted", "elspeth.web.composer.provider_quota", "V4SdkTerminalCommitted"],
        ["V4SdkFailureObservation", "elspeth.web.composer.provider_sdk_observation", "V4SdkFailureObservation"],
        ["capture_sdk_terminal_failure", "elspeth.web.composer.provider_sdk_terminal", "capture_sdk_terminal_failure"],
        ["V4ProviderResultCompleteness", "elspeth.web.composer.provider_result_admission", "V4ProviderResultCompleteness"],
        ["V4ProviderResultIncomplete", "elspeth.web.composer.provider_result_admission", "V4ProviderResultIncomplete"],
        ["ComposerServiceImpl", "elspeth.web.composer.service", "ComposerServiceImpl"],
        ["ComposerAsyncOperationAuthority", "elspeth.web.coordination.composer_operation_authority", "ComposerAsyncOperationAuthority"],
        ["RequiredSQLFinishOnce", "elspeth.web.required_sql_outcomes", "RequiredSQLFinishOnce"],
        ["RequiredSQLRaised", "elspeth.web.required_sql_outcomes", "RequiredSQLRaised"],
        ["RequiredSQLReturned", "elspeth.web.required_sql_outcomes", "RequiredSQLReturned"],
        ["RequiredWorkBinding", "elspeth.web.required_work", "RequiredWorkBinding"],
        ["RequiredWorkSource", "elspeth.web.required_work", "RequiredWorkSource"],
        ["RequiredWorkTicket", "elspeth.web.required_work", "RequiredWorkTicket"],
        ["PreparedContinuationProvider", "elspeth.web.sessions.continuation", "PreparedContinuationProvider"],
        ["VerifiedPaidIncompleteTerminal", "elspeth.web.sessions.continuation_paid_incomplete_terminal", "VerifiedPaidIncompleteTerminal"],
        ["PrimaryNoSdkRefusalReceipt", "elspeth.web.sessions.continuation_primary", "PrimaryNoSdkRefusalReceipt"],
        ["PrimaryPaidIncompleteTerminal", "elspeth.web.sessions.continuation_primary", "PrimaryPaidIncompleteTerminal"],
        ["PrimaryPaidOverflowTerminal", "elspeth.web.sessions.continuation_primary", "PrimaryPaidOverflowTerminal"],
        ["PrimaryProviderResult", "elspeth.web.sessions.continuation_primary", "PrimaryProviderResult"],
        ["V4PrimaryProviderAdmission", "elspeth.web.sessions.continuation_primary", "V4PrimaryProviderAdmission"],
        ["PrimarySdkTerminal", "elspeth.web.sessions.continuation_sdk_terminal_result", "PrimarySdkTerminal"],
        ["SdkTerminalFailure", "elspeth.web.sessions.continuation_sdk_terminal_result", "SdkTerminalFailure"],
        ["VerifiedSdkTerminal", "elspeth.web.sessions.continuation_sdk_terminal_result", "VerifiedSdkTerminal"],
        ["SessionServiceImpl", "elspeth.web.sessions.service", "SessionServiceImpl"],
    ],
    "src/elspeth/web/composer/continuation_title_provider.py": [
        ["asyncio", "asyncio", None],
        ["AuditIntegrityError", "elspeth.contracts.errors", "AuditIntegrityError"],
        ["run_required_sql_finish_once", "elspeth.web.async_workers", "run_required_sql_finish_once"],
        ["InitializeFrame", "elspeth.web.composer.continuation", "InitializeFrame"],
        ["decode_continuation_snapshot", "elspeth.web.composer.continuation_codec", "decode_continuation_snapshot"],
        ["_registered", "elspeth.web.composer.continuation_initialization", "_registered"],
        ["_sessions", "elspeth.web.composer.continuation_initialization", "_sessions"],
        ["decode_continuation_policy", "elspeth.web.composer.continuation_policy", "decode_continuation_policy"],
        ["TitleNoSdkIssuer", "elspeth.web.composer.continuation_title_no_sdk", "TitleNoSdkIssuer"],
        ["ProviderCallCustody", "elspeth.web.composer.provider_quota", "ProviderCallCustody"],
        ["ProviderInvocationFamily", "elspeth.web.composer.provider_quota", "ProviderInvocationFamily"],
        ["ProviderInvocationOwner", "elspeth.web.composer.provider_quota", "ProviderInvocationOwner"],
        ["V4SdkTerminalCommitted", "elspeth.web.composer.provider_quota", "V4SdkTerminalCommitted"],
        ["V4SdkFailureObservation", "elspeth.web.composer.provider_sdk_observation", "V4SdkFailureObservation"],
        ["capture_sdk_terminal_failure", "elspeth.web.composer.provider_sdk_terminal", "capture_sdk_terminal_failure"],
        ["V4ProviderResultIncomplete", "elspeth.web.composer.provider_result_admission", "V4ProviderResultIncomplete"],
        ["RequiredSQLFinishOnce", "elspeth.web.required_sql_outcomes", "RequiredSQLFinishOnce"],
        ["RequiredSQLRaised", "elspeth.web.required_sql_outcomes", "RequiredSQLRaised"],
        ["RequiredSQLReturned", "elspeth.web.required_sql_outcomes", "RequiredSQLReturned"],
        ["RequiredWorkSource", "elspeth.web.required_work", "RequiredWorkSource"],
        ["RequiredWorkTicket", "elspeth.web.required_work", "RequiredWorkTicket"],
        ["_maybe_auto_title_session_required", "elspeth.web.sessions._auto_title", "_maybe_auto_title_session_required"],
        ["_AutoTitleCapture", "elspeth.web.sessions.auto_title_outcome", "_AutoTitleCapture"],
        ["SdkTerminalFamily", "elspeth.web.sessions.continuation_sdk_tail", "SdkTerminalFamily"],
        ["SdkTerminalFailure", "elspeth.web.sessions.continuation_sdk_terminal_result", "SdkTerminalFailure"],
        ["SdkTerminalResult", "elspeth.web.sessions.continuation_sdk_terminal_result", "SdkTerminalResult"],
        ["VerifiedSdkTerminal", "elspeth.web.sessions.continuation_sdk_terminal_result", "VerifiedSdkTerminal"],
        ["PreparedTitleProvider", "elspeth.web.sessions.continuation_title_provider", "PreparedTitleProvider"],
        ["TitleAuditSettlementRequest", "elspeth.web.sessions.continuation_title_provider", "TitleAuditSettlementRequest"],
        ["_current", "elspeth.web.sessions.continuation_title_provider", "_current"],
        ["locked_session_transaction", "elspeth.web.sessions.locking", "locked_session_transaction"],
        ["SendMessageRequest", "elspeth.web.sessions.schemas", "SendMessageRequest"],
    ],
    "src/elspeth/web/sessions/continuation_classifier_primary.py": [
        ["asyncio", "asyncio", None],
        ["time", "time", None],
        ["replace", "dataclasses", "replace"],
        ["ComposerLLMCall", "elspeth.contracts.composer_llm_audit", "ComposerLLMCall"],
        ["AuditIntegrityError", "elspeth.contracts.errors", "AuditIntegrityError"],
        ["run_required_sql_finish_once", "elspeth.web.async_workers", "run_required_sql_finish_once"],
        ["_CallModelOutcome", "elspeth.web.composer._compose_loop_carriers", "_CallModelOutcome"],
        ["BufferingRecorder", "elspeth.web.composer.audit", "BufferingRecorder"],
        ["ClassifierNoSdkIssuer", "elspeth.web.composer.continuation_classifier_no_sdk", "ClassifierNoSdkIssuer"],
        ["ClassifierNoSdkRefusalReason", "elspeth.web.composer.continuation_classifier_no_sdk", "ClassifierNoSdkRefusalReason"],
        ["ContinuationActionClock", "elspeth.web.composer.continuation_clock", "ContinuationActionClock"],
        ["CurrentContinuationEnvInventory", "elspeth.web.composer.continuation_env_inventory", "CurrentContinuationEnvInventory"],
        ["decode_continuation_policy", "elspeth.web.composer.continuation_policy", "decode_continuation_policy"],
        ["RestoredContinuationRuntime", "elspeth.web.composer.continuation_runtime", "RestoredContinuationRuntime"],
        ["SelectedContinuationState", "elspeth.web.composer.continuation_selection", "SelectedContinuationState"],
        ["PreparedProviderInput", "elspeth.web.composer.prepared_provider_input", "PreparedProviderInput"],
        ["PreparedContinuationPrimaryRequest", "elspeth.web.composer.provider_gateway", "PreparedContinuationPrimaryRequest"],
        ["ProviderInvocationFamily", "elspeth.web.composer.provider_quota", "ProviderInvocationFamily"],
        ["ProviderInvocationOwner", "elspeth.web.composer.provider_quota", "ProviderInvocationOwner"],
        ["CompositionState", "elspeth.web.composer.state", "CompositionState"],
        ["RequiredSQLFinishOnce", "elspeth.web.required_sql_outcomes", "RequiredSQLFinishOnce"],
        ["RequiredSQLRaised", "elspeth.web.required_sql_outcomes", "RequiredSQLRaised"],
        ["RequiredSQLReturned", "elspeth.web.required_sql_outcomes", "RequiredSQLReturned"],
        ["RequiredWorkBinding", "elspeth.web.required_work", "RequiredWorkBinding"],
        ["RequiredWorkSource", "elspeth.web.required_work", "RequiredWorkSource"],
        ["RequiredWorkTicket", "elspeth.web.required_work", "RequiredWorkTicket"],
        ["ComposerOperationRunning", "elspeth.web.sessions.composer_operations", "ComposerOperationRunning"],
        ["VerifiedPaidIncompleteTerminal", "elspeth.web.sessions.continuation_paid_incomplete_terminal", "VerifiedPaidIncompleteTerminal"],
        ["ContinuationSQLDeadlineOwner", "elspeth.web.sessions.continuation_sql_deadline", "ContinuationSQLDeadlineOwner"],
        ["SdkTerminalFamily", "elspeth.web.sessions.continuation_sdk_tail", "SdkTerminalFamily"],
        ["SdkTerminalFailure", "elspeth.web.sessions.continuation_sdk_terminal_result", "SdkTerminalFailure"],
        ["SdkTerminalResult", "elspeth.web.sessions.continuation_sdk_terminal_result", "SdkTerminalResult"],
        ["VerifiedSdkTerminal", "elspeth.web.sessions.continuation_sdk_terminal_result", "VerifiedSdkTerminal"],
        ["locked_session_transaction", "elspeth.web.sessions.locking", "locked_session_transaction"],
    ],
    "src/elspeth/web/composer/continuation_primary_no_sdk.py": [
        ["asyncio", "asyncio", None],
        ["dataclass", "dataclasses", "dataclass"],
        ["RLock", "threading", "RLock"],
        ["uuid4", "uuid", "uuid4"],
        ["SQLAlchemyError", "sqlalchemy.exc", "SQLAlchemyError"],
        ["ChargeableAdmissionRefused", "elspeth.contracts.chargeable_admission", "ChargeableAdmissionRefused"],
        ["AuditIntegrityError", "elspeth.contracts.errors", "AuditIntegrityError"],
        ["ContinuationActionClock", "elspeth.web.composer.continuation_clock", "ContinuationActionClock"],
        ["CommonProviderCostRefused", "elspeth.web.composer.provider_cost", "CommonProviderCostRefused"],
        ["ProviderAttempt", "elspeth.web.coordination.quota_authority", "ProviderAttempt"],
        ["RequiredWorkBinding", "elspeth.web.required_work", "RequiredWorkBinding"],
        ["ComposerOperationRunning", "elspeth.web.sessions.composer_operations", "ComposerOperationRunning"],
        ["PreparedContinuationProvider", "elspeth.web.sessions.continuation", "PreparedContinuationProvider"],
        ["StoredContinuationSnapshot", "elspeth.web.sessions.continuation", "StoredContinuationSnapshot"],
    ],
    "src/elspeth/web/composer/continuation_classifier_no_sdk.py": [
        ["asyncio", "asyncio", None],
        ["dataclass", "dataclasses", "dataclass"],
        ["RLock", "threading", "RLock"],
        ["uuid4", "uuid", "uuid4"],
        ["SQLAlchemyError", "sqlalchemy.exc", "SQLAlchemyError"],
        ["ChargeableAdmissionRefused", "elspeth.contracts.chargeable_admission", "ChargeableAdmissionRefused"],
        ["AuditIntegrityError", "elspeth.contracts.errors", "AuditIntegrityError"],
        ["ContinuationActionClock", "elspeth.web.composer.continuation_clock", "ContinuationActionClock"],
        ["ContinuationRetainedTailUnbounded", "elspeth.web.composer.continuation_primary_no_sdk", "ContinuationRetainedTailUnbounded"],
        ["CommonProviderCostRefused", "elspeth.web.composer.provider_cost", "CommonProviderCostRefused"],
        ["ProviderAttempt", "elspeth.web.coordination.quota_authority", "ProviderAttempt"],
        ["RequiredWorkBinding", "elspeth.web.required_work", "RequiredWorkBinding"],
        ["ComposerOperationRunning", "elspeth.web.sessions.composer_operations", "ComposerOperationRunning"],
        ["StoredContinuationSnapshot", "elspeth.web.sessions.continuation", "StoredContinuationSnapshot"],
    ],
    "src/elspeth/web/composer/continuation_title_no_sdk.py": [
        ["asyncio", "asyncio", None],
        ["dataclass", "dataclasses", "dataclass"],
        ["RLock", "threading", "RLock"],
        ["uuid4", "uuid", "uuid4"],
        ["ChargeableAdmissionRefused", "elspeth.contracts.chargeable_admission", "ChargeableAdmissionRefused"],
        ["AuditIntegrityError", "elspeth.contracts.errors", "AuditIntegrityError"],
        ["PromptGuardRefused", "elspeth.web.composer.prompt_guard", "PromptGuardRefused"],
        ["CommonProviderCostRefused", "elspeth.web.composer.provider_cost", "CommonProviderCostRefused"],
        ["RequiredWorkRole", "elspeth.web.required_work", "RequiredWorkRole"],
    ],
    "src/elspeth/web/composer/provider_quota.py": [["AuditIntegrityError", "elspeth.contracts.errors", "AuditIntegrityError"]],
    "src/elspeth/web/sessions/service.py": [
        ["datetime", "datetime", "datetime"],
        ["AuditIntegrityError", "elspeth.contracts.errors", "AuditIntegrityError"],
        ["run_required_sql_finish_once", "elspeth.web.async_workers", "run_required_sql_finish_once"],
        ["SessionOperationKind", "elspeth.web.coordination.contracts", "SessionOperationKind"],
        ["RequiredSQLFinishOnce", "elspeth.web.required_sql_outcomes", "RequiredSQLFinishOnce"],
        ["RequiredWorkSource", "elspeth.web.required_work", "RequiredWorkSource"],
        ["RequiredWorkTicket", "elspeth.web.required_work", "RequiredWorkTicket"],
    ],
    "src/elspeth/web/required_work.py": [
        ["AuditIntegrityError", "elspeth.contracts.errors", "AuditIntegrityError"],
        ["RequiredSQLFinishOnce", "elspeth.web.required_sql_outcomes", "RequiredSQLFinishOnce"],
    ],
    "src/elspeth/web/coordination/composer_operation_authority.py": [
        ["AuditIntegrityError", "elspeth.contracts.errors", "AuditIntegrityError"],
        ["locked_session_transaction", "elspeth.web.sessions.locking", "locked_session_transaction"],
    ],
    "src/elspeth/web/sessions/continuation_primary.py": [
        ["Callable", "collections.abc", "Callable"],
        ["datetime", "datetime", "datetime"],
        ["sha256", "hashlib", "sha256"],
        ["UUID", "uuid", "UUID"],
        ["ValidationError", "pydantic", "ValidationError"],
        ["Connection", "sqlalchemy", "Connection"],
        ["select", "sqlalchemy", "select"],
        ["AuditIntegrityError", "elspeth.contracts.errors", "AuditIntegrityError"],
        ["canonical_json", "elspeth.contracts.hashing", "canonical_json"],
        ["ComposeFrame", "elspeth.web.composer.continuation", "ComposeFrame"],
        ["decode_continuation_snapshot", "elspeth.web.composer.continuation_codec", "decode_continuation_snapshot"],
        ["encode_continuation_snapshot", "elspeth.web.composer.continuation_codec", "encode_continuation_snapshot"],
        ["encode_private_chunk", "elspeth.web.composer.continuation_codec", "encode_private_chunk"],
        ["verify_private_chunk", "elspeth.web.composer.continuation_codec", "verify_private_chunk"],
        ["ContinuationAdmissionPolicyV4", "elspeth.web.composer.continuation_policy", "ContinuationAdmissionPolicyV4"],
        ["decode_continuation_policy", "elspeth.web.composer.continuation_policy", "decode_continuation_policy"],
        ["PrimaryNoSdkRefusalPermit", "elspeth.web.composer.continuation_primary_no_sdk", "PrimaryNoSdkRefusalPermit"],
        ["PrimaryNoSdkRefusalReason", "elspeth.web.composer.continuation_primary_no_sdk", "PrimaryNoSdkRefusalReason"],
        ["PromptGuardRefused", "elspeth.web.composer.prompt_guard", "PromptGuardRefused"],
        ["_undispatched_cancellation_event_id", "elspeth.web.coordination.quota_authority", "_undispatched_cancellation_event_id"],
        ["_usage_entry_id", "elspeth.web.coordination.quota_authority", "_usage_entry_id"],
        ["ComposerOperationError", "elspeth.web.sessions.composer_operations", "ComposerOperationError"],
        ["PreparedContinuationProvider", "elspeth.web.sessions.continuation", "PreparedContinuationProvider"],
        ["StoredContinuationSnapshot", "elspeth.web.sessions.continuation", "StoredContinuationSnapshot"],
        ["private_chunk_key", "elspeth.web.sessions.continuation", "private_chunk_key"],
        ["read_chunk", "elspeth.web.sessions.continuation", "read_chunk"],
        ["require_snapshot_cas", "elspeth.web.sessions.continuation", "require_snapshot_cas"],
        ["messages", "elspeth.web.sessions.models", "chat_messages_table"],
        ["jobs", "elspeth.web.sessions.models", "composer_async_operations_table"],
        ["checkpoints", "elspeth.web.sessions.models", "composer_continuation_checkpoints_table"],
        ["chunks", "elspeth.web.sessions.models", "composer_continuation_chunks_table"],
        ["events", "elspeth.web.sessions.models", "composer_continuation_events_table"],
        ["physical", "elspeth.web.sessions.models", "quota_provider_attempts_table"],
        ["sessions_table", "elspeth.web.sessions.models", "sessions_table"],
        ["usage", "elspeth.web.sessions.models", "token_usage_ledger_table"],
        ["restore_utc", "elspeth.web.sessions.time_normalization", "restore_utc"],
    ],
    "src/elspeth/web/sessions/continuation_primary_paid_overflow_refusal.py": [
        ["Callable", "collections.abc", "Callable"],
        ["datetime", "datetime", "datetime"],
        ["Connection", "sqlalchemy", "Connection"],
        ["select", "sqlalchemy", "select"],
        ["update", "sqlalchemy", "update"],
        ["AuditIntegrityError", "elspeth.contracts.errors", "AuditIntegrityError"],
        ["decode_continuation_snapshot", "elspeth.web.composer.continuation_codec", "decode_continuation_snapshot"],
        ["PrimaryNoSdkRefusalPermit", "elspeth.web.composer.continuation_primary_no_sdk", "PrimaryNoSdkRefusalPermit"],
        ["_record_from_row", "elspeth.web.coordination.composer_operation_authority", "_record_from_row"],
        ["_terminal_values", "elspeth.web.coordination.composer_operation_authority", "_terminal_values"],
        ["database_now", "elspeth.web.coordination.database_clock", "database_now"],
        [
            "cancel_undispatched_provider_attempt_on_connection",
            "elspeth.web.coordination.quota_authority",
            "cancel_undispatched_provider_attempt_on_connection",
        ],
        ["PreparedContinuationProvider", "elspeth.web.sessions.continuation", "PreparedContinuationProvider"],
        ["_effect_snapshot", "elspeth.web.sessions.continuation", "_effect_snapshot"],
        ["_finalize_terminal_rows_after_append", "elspeth.web.sessions.continuation", "_finalize_terminal_rows_after_append"],
        ["append_complete_snapshot", "elspeth.web.sessions.continuation", "append_complete_snapshot"],
        ["put_chunk", "elspeth.web.sessions.continuation", "put_chunk"],
        ["PrimaryNoSdkRefusalReceipt", "elspeth.web.sessions.continuation_primary", "PrimaryNoSdkRefusalReceipt"],
        ["V4PrimaryProviderAdmission", "elspeth.web.sessions.continuation_primary", "V4PrimaryProviderAdmission"],
        ["_selected_effect", "elspeth.web.sessions.continuation_primary", "_selected_effect"],
        ["_v4_dispatch_action_projection", "elspeth.web.sessions.continuation_primary", "_v4_dispatch_action_projection"],
        ["stored_primary_input_data_on_connection", "elspeth.web.sessions.continuation_primary", "stored_primary_input_data_on_connection"],
        ["V4PrimaryResultBatch", "elspeth.web.sessions.continuation_primary_paid_overflow_capacity", "V4PrimaryResultBatch"],
        [
            "admit_v4_primary_result_batch_on_connection",
            "elspeth.web.sessions.continuation_primary_paid_overflow_capacity",
            "admit_v4_primary_result_batch_on_connection",
        ],
        [
            "record_primary_hold_consumed",
            "elspeth.web.sessions.continuation_primary_paid_overflow_capacity",
            "record_primary_hold_consumed",
        ],
        [
            "build_primary_no_sdk_tail_batch",
            "elspeth.web.sessions.continuation_primary_paid_overflow_no_sdk",
            "build_primary_no_sdk_tail_batch",
        ],
        [
            "fixed_v4_primary_no_sdk_error",
            "elspeth.web.sessions.continuation_primary_paid_overflow_no_sdk",
            "fixed_v4_primary_no_sdk_error",
        ],
        [
            "prepare_primary_no_sdk_tail_plan",
            "elspeth.web.sessions.continuation_primary_paid_overflow_no_sdk",
            "prepare_primary_no_sdk_tail_plan",
        ],
        [
            "prepare_primary_paid_overflow_tail_plan",
            "elspeth.web.sessions.continuation_primary_paid_overflow_tail",
            "prepare_primary_paid_overflow_tail_plan",
        ],
        ["primary_terminal_reservation", "elspeth.web.sessions.continuation_primary_terminal_reservation", "primary_terminal_reservation"],
        ["_require_historical_hold_source", "elspeth.web.sessions.continuation_retained_capacity", "_require_historical_hold_source"],
        ["_require_hold_fields", "elspeth.web.sessions.continuation_retained_capacity", "_require_hold_fields"],
        ["jobs", "elspeth.web.sessions.models", "composer_async_operations_table"],
        ["effects", "elspeth.web.sessions.models", "composer_continuation_effects_table"],
        ["segments", "elspeth.web.sessions.models", "composer_operation_segments_table"],
        ["holds", "elspeth.web.sessions.models", "composer_provider_terminal_holds_table"],
        ["attempts", "elspeth.web.sessions.models", "composer_segment_attempts_table"],
        ["physical", "elspeth.web.sessions.models", "quota_provider_attempts_table"],
    ],
    "src/elspeth/web/sessions/continuation_primary_paid_overflow_refusal_terminal.py": [
        ["UUID", "uuid", "UUID"],
        ["ValidationError", "pydantic", "ValidationError"],
        ["Connection", "sqlalchemy", "Connection"],
        ["select", "sqlalchemy", "select"],
        ["AuditIntegrityError", "elspeth.contracts.errors", "AuditIntegrityError"],
        ["canonical_json", "elspeth.contracts.hashing", "canonical_json"],
        ["decode_continuation_snapshot", "elspeth.web.composer.continuation_codec", "decode_continuation_snapshot"],
        ["encode_continuation_snapshot", "elspeth.web.composer.continuation_codec", "encode_continuation_snapshot"],
        ["encode_private_chunk", "elspeth.web.composer.continuation_codec", "encode_private_chunk"],
        ["PrimaryNoSdkRefusalReason", "elspeth.web.composer.continuation_primary_no_sdk", "PrimaryNoSdkRefusalReason"],
        ["_record_from_row", "elspeth.web.coordination.composer_operation_authority", "_record_from_row"],
        ["_undispatched_cancellation_event_id", "elspeth.web.coordination.quota_authority", "_undispatched_cancellation_event_id"],
        ["_usage_entry_id", "elspeth.web.coordination.quota_authority", "_usage_entry_id"],
        ["composer_operation_result_hash", "elspeth.web.sessions.composer_operations", "composer_operation_result_hash"],
        ["StoredContinuationSnapshot", "elspeth.web.sessions.continuation", "StoredContinuationSnapshot"],
        ["load_snapshot", "elspeth.web.sessions.continuation", "load_snapshot"],
        ["private_chunk_key", "elspeth.web.sessions.continuation", "private_chunk_key"],
        ["PrimaryNoSdkRefusalReceipt", "elspeth.web.sessions.continuation_primary", "PrimaryNoSdkRefusalReceipt"],
        ["_primary_frame", "elspeth.web.sessions.continuation_primary", "_primary_frame"],
        ["_PrimaryNoSdkRefusalData", "elspeth.web.sessions.continuation_primary", "_PrimaryNoSdkRefusalData"],
        ["_v4_dispatch_action_projection", "elspeth.web.sessions.continuation_primary", "_v4_dispatch_action_projection"],
        ["stored_primary_input_data_on_connection", "elspeth.web.sessions.continuation_primary", "stored_primary_input_data_on_connection"],
        ["_shape", "elspeth.web.sessions.continuation_primary_paid_overflow_no_sdk", "_shape"],
        [
            "fixed_v4_primary_no_sdk_error",
            "elspeth.web.sessions.continuation_primary_paid_overflow_no_sdk",
            "fixed_v4_primary_no_sdk_error",
        ],
        [
            "prepare_primary_no_sdk_tail_plan",
            "elspeth.web.sessions.continuation_primary_paid_overflow_no_sdk",
            "prepare_primary_no_sdk_tail_plan",
        ],
        [
            "require_original_v4_primary_physical_source_on_connection",
            "elspeth.web.sessions.continuation_primary_paid_overflow_source",
            "require_original_v4_primary_physical_source_on_connection",
        ],
        [
            "prepare_primary_paid_overflow_tail_plan",
            "elspeth.web.sessions.continuation_primary_paid_overflow_tail",
            "prepare_primary_paid_overflow_tail_plan",
        ],
        ["_bounded_bytes", "elspeth.web.sessions.continuation_primary_paid_overflow_terminal", "_bounded_bytes"],
        ["_stored_hold_source", "elspeth.web.sessions.continuation_primary_paid_overflow_terminal", "_stored_hold_source"],
        ["primary_terminal_reservation", "elspeth.web.sessions.continuation_primary_terminal_reservation", "primary_terminal_reservation"],
        ["_require_historical_hold_source", "elspeth.web.sessions.continuation_retained_capacity", "_require_historical_hold_source"],
        ["_require_hold_fields", "elspeth.web.sessions.continuation_retained_capacity", "_require_hold_fields"],
        ["messages", "elspeth.web.sessions.models", "chat_messages_table"],
        ["jobs", "elspeth.web.sessions.models", "composer_async_operations_table"],
        ["checkpoints", "elspeth.web.sessions.models", "composer_continuation_checkpoints_table"],
        ["effects", "elspeth.web.sessions.models", "composer_continuation_effects_table"],
        ["events", "elspeth.web.sessions.models", "composer_continuation_events_table"],
        ["physical", "elspeth.web.sessions.models", "quota_provider_attempts_table"],
        ["usage", "elspeth.web.sessions.models", "token_usage_ledger_table"],
        ["restore_utc", "elspeth.web.sessions.time_normalization", "restore_utc"],
    ],
    "src/elspeth/web/sessions/continuation_title_no_sdk_close.py": [
        ["datetime", "datetime", "datetime"],
        ["Connection", "sqlalchemy", "Connection"],
        ["select", "sqlalchemy", "select"],
        ["update", "sqlalchemy", "update"],
        ["AuditIntegrityError", "elspeth.contracts.errors", "AuditIntegrityError"],
        ["TitleNoSdkRefusalPermit", "elspeth.web.composer.continuation_title_no_sdk", "TitleNoSdkRefusalPermit"],
        ["_record_from_row", "elspeth.web.coordination.composer_operation_authority", "_record_from_row"],
        ["_terminal_values", "elspeth.web.coordination.composer_operation_authority", "_terminal_values"],
        ["database_now", "elspeth.web.coordination.database_clock", "database_now"],
        [
            "cancel_undispatched_provider_attempt_on_connection",
            "elspeth.web.coordination.quota_authority",
            "cancel_undispatched_provider_attempt_on_connection",
        ],
        ["ComposerOperationRecord", "elspeth.web.sessions.composer_operations", "ComposerOperationRecord"],
        ["_finalize_terminal_rows_after_append", "elspeth.web.sessions.continuation", "_finalize_terminal_rows_after_append"],
        ["append_complete_snapshot", "elspeth.web.sessions.continuation", "append_complete_snapshot"],
        ["put_chunk", "elspeth.web.sessions.continuation", "put_chunk"],
        ["_require_historical_hold_source", "elspeth.web.sessions.continuation_retained_capacity", "_require_historical_hold_source"],
        ["_require_hold_fields", "elspeth.web.sessions.continuation_retained_capacity", "_require_hold_fields"],
        ["build_title_no_sdk_tail_batch", "elspeth.web.sessions.continuation_title_no_sdk_tail", "build_title_no_sdk_tail_batch"],
        [
            "read_v4_title_no_sdk_terminal_on_connection",
            "elspeth.web.sessions.continuation_title_no_sdk_terminal",
            "read_v4_title_no_sdk_terminal_on_connection",
        ],
        ["PreparedTitleProvider", "elspeth.web.sessions.continuation_title_provider", "PreparedTitleProvider"],
        ["V4TitleProviderAdmission", "elspeth.web.sessions.continuation_title_provider", "V4TitleProviderAdmission"],
        ["_current", "elspeth.web.sessions.continuation_title_provider", "_current"],
        ["_source_chain", "elspeth.web.sessions.continuation_title_provider", "_source_chain"],
        [
            "admit_v4_title_no_sdk_tail_on_connection",
            "elspeth.web.sessions.continuation_v4_paid_tail",
            "admit_v4_title_no_sdk_tail_on_connection",
        ],
        ["record_paid_tail_hold_consumed", "elspeth.web.sessions.continuation_v4_paid_tail", "record_paid_tail_hold_consumed"],
        ["jobs", "elspeth.web.sessions.models", "composer_async_operations_table"],
        ["effects", "elspeth.web.sessions.models", "composer_continuation_effects_table"],
        ["segments", "elspeth.web.sessions.models", "composer_operation_segments_table"],
        ["holds", "elspeth.web.sessions.models", "composer_provider_terminal_holds_table"],
        ["attempts", "elspeth.web.sessions.models", "composer_segment_attempts_table"],
        ["physical", "elspeth.web.sessions.models", "quota_provider_attempts_table"],
    ],
    "src/elspeth/web/sessions/continuation_title_no_sdk_terminal.py": [
        ["sha256", "hashlib", "sha256"],
        ["UUID", "uuid", "UUID"],
        ["Connection", "sqlalchemy", "Connection"],
        ["select", "sqlalchemy", "select"],
        ["AuditIntegrityError", "elspeth.contracts.errors", "AuditIntegrityError"],
        ["canonical_json", "elspeth.contracts.hashing", "canonical_json"],
        ["parse_json_strict", "elspeth.contracts.json_parser", "parse_json_strict"],
        ["decode_continuation_snapshot", "elspeth.web.composer.continuation_codec", "decode_continuation_snapshot"],
        ["TitleNoSdkRefusalReason", "elspeth.web.composer.continuation_title_no_sdk", "TitleNoSdkRefusalReason"],
        ["_undispatched_cancellation_event_id", "elspeth.web.coordination.quota_authority", "_undispatched_cancellation_event_id"],
        ["_usage_entry_id", "elspeth.web.coordination.quota_authority", "_usage_entry_id"],
        ["COMPOSER_OPERATION_RESULT_SCHEMA_ERROR", "elspeth.web.sessions.composer_operations", "COMPOSER_OPERATION_RESULT_SCHEMA_ERROR"],
        ["composer_operation_result_hash", "elspeth.web.sessions.composer_operations", "composer_operation_result_hash"],
        ["StoredContinuationSnapshot", "elspeth.web.sessions.continuation", "StoredContinuationSnapshot"],
        ["load_snapshot", "elspeth.web.sessions.continuation", "load_snapshot"],
        ["PaidOverflowIdentity", "elspeth.web.sessions.continuation_paid_overflow", "PaidOverflowIdentity"],
        ["_require_historical_hold_source", "elspeth.web.sessions.continuation_retained_capacity", "_require_historical_hold_source"],
        ["_require_hold_fields", "elspeth.web.sessions.continuation_retained_capacity", "_require_hold_fields"],
        [
            "require_original_v4_title_physical_source_on_connection",
            "elspeth.web.sessions.continuation_title_paid_overflow_source",
            "require_original_v4_title_physical_source_on_connection",
        ],
        [
            "prepare_title_paid_overflow_tail_plan",
            "elspeth.web.sessions.continuation_title_paid_overflow_tail",
            "prepare_title_paid_overflow_tail_plan",
        ],
        ["_bounded_bytes", "elspeth.web.sessions.continuation_title_paid_overflow_terminal", "_bounded_bytes"],
        ["_stored_hold_source", "elspeth.web.sessions.continuation_title_paid_overflow_terminal", "_stored_hold_source"],
        ["messages", "elspeth.web.sessions.models", "chat_messages_table"],
        ["jobs", "elspeth.web.sessions.models", "composer_async_operations_table"],
        ["checkpoints", "elspeth.web.sessions.models", "composer_continuation_checkpoints_table"],
        ["effects", "elspeth.web.sessions.models", "composer_continuation_effects_table"],
        ["events", "elspeth.web.sessions.models", "composer_continuation_events_table"],
        ["segments", "elspeth.web.sessions.models", "composer_operation_segments_table"],
        ["attempts", "elspeth.web.sessions.models", "composer_segment_attempts_table"],
        ["physical", "elspeth.web.sessions.models", "quota_provider_attempts_table"],
        ["ledger", "elspeth.web.sessions.models", "token_usage_ledger_table"],
        ["restore_utc", "elspeth.web.sessions.time_normalization", "restore_utc"],
    ],
    "src/elspeth/web/sessions/continuation_classifier_primary_no_sdk.py": [
        ["Callable", "collections.abc", "Callable"],
        ["datetime", "datetime", "datetime"],
        ["sha256", "hashlib", "sha256"],
        ["UUID", "uuid", "UUID"],
        ["ValidationError", "pydantic", "ValidationError"],
        ["Connection", "sqlalchemy", "Connection"],
        ["select", "sqlalchemy", "select"],
        ["update", "sqlalchemy", "update"],
        ["AuditIntegrityError", "elspeth.contracts.errors", "AuditIntegrityError"],
        ["canonical_json", "elspeth.contracts.hashing", "canonical_json"],
        ["ClassifierNoSdkRefusalPermit", "elspeth.web.composer.continuation_classifier_no_sdk", "ClassifierNoSdkRefusalPermit"],
        ["ClassifierNoSdkRefusalReason", "elspeth.web.composer.continuation_classifier_no_sdk", "ClassifierNoSdkRefusalReason"],
        ["decode_continuation_snapshot", "elspeth.web.composer.continuation_codec", "decode_continuation_snapshot"],
        ["encode_continuation_snapshot", "elspeth.web.composer.continuation_codec", "encode_continuation_snapshot"],
        ["encode_private_chunk", "elspeth.web.composer.continuation_codec", "encode_private_chunk"],
        ["applied_batch_cut", "elspeth.web.composer.continuation_iteration_transitions", "applied_batch_cut"],
        ["_record_from_row", "elspeth.web.coordination.composer_operation_authority", "_record_from_row"],
        ["_terminal_values", "elspeth.web.coordination.composer_operation_authority", "_terminal_values"],
        ["database_now", "elspeth.web.coordination.database_clock", "database_now"],
        ["_undispatched_cancellation_event_id", "elspeth.web.coordination.quota_authority", "_undispatched_cancellation_event_id"],
        ["_usage_entry_id", "elspeth.web.coordination.quota_authority", "_usage_entry_id"],
        [
            "cancel_undispatched_provider_attempt_on_connection",
            "elspeth.web.coordination.quota_authority",
            "cancel_undispatched_provider_attempt_on_connection",
        ],
        ["composer_operation_result_hash", "elspeth.web.sessions.composer_operations", "composer_operation_result_hash"],
        ["StoredContinuationSnapshot", "elspeth.web.sessions.continuation", "StoredContinuationSnapshot"],
        ["_finalize_terminal_rows_after_append", "elspeth.web.sessions.continuation", "_finalize_terminal_rows_after_append"],
        ["append_complete_snapshot", "elspeth.web.sessions.continuation", "append_complete_snapshot"],
        ["load_snapshot", "elspeth.web.sessions.continuation", "load_snapshot"],
        ["private_chunk_key", "elspeth.web.sessions.continuation", "private_chunk_key"],
        ["put_chunk", "elspeth.web.sessions.continuation", "put_chunk"],
        ["_chain", "elspeth.web.sessions.continuation_classifier_primary", "_chain"],
        ["_read_source", "elspeth.web.sessions.continuation_classifier_primary", "_read_source"],
        [
            "prepare_classifier_paid_incomplete_tail_plan",
            "elspeth.web.sessions.continuation_classifier_primary_incomplete_tail",
            "prepare_classifier_paid_incomplete_tail_plan",
        ],
        [
            "prepare_classifier_paid_overflow_tail_plan",
            "elspeth.web.sessions.continuation_classifier_primary_paid_tail",
            "prepare_classifier_paid_overflow_tail_plan",
        ],
        [
            "ClassifierTerminalReservation",
            "elspeth.web.sessions.continuation_classifier_primary_terminal_reservation",
            "ClassifierTerminalReservation",
        ],
        [
            "classifier_sdk_terminal_reservation",
            "elspeth.web.sessions.continuation_classifier_primary_terminal_reservation",
            "classifier_sdk_terminal_reservation",
        ],
        ["V4PrimaryResultBatch", "elspeth.web.sessions.continuation_primary_paid_overflow_capacity", "V4PrimaryResultBatch"],
        [
            "admit_v4_primary_result_batch_on_connection",
            "elspeth.web.sessions.continuation_primary_paid_overflow_capacity",
            "admit_v4_primary_result_batch_on_connection",
        ],
        [
            "record_primary_hold_consumed",
            "elspeth.web.sessions.continuation_primary_paid_overflow_capacity",
            "record_primary_hold_consumed",
        ],
        ["_bounded_bytes", "elspeth.web.sessions.continuation_primary_paid_overflow_terminal", "_bounded_bytes"],
        ["_stored_hold_source", "elspeth.web.sessions.continuation_primary_paid_overflow_terminal", "_stored_hold_source"],
        ["_require_historical_hold_source", "elspeth.web.sessions.continuation_retained_capacity", "_require_historical_hold_source"],
        ["_require_hold_fields", "elspeth.web.sessions.continuation_retained_capacity", "_require_hold_fields"],
        ["messages", "elspeth.web.sessions.models", "chat_messages_table"],
        ["jobs", "elspeth.web.sessions.models", "composer_async_operations_table"],
        ["checkpoints", "elspeth.web.sessions.models", "composer_continuation_checkpoints_table"],
        ["effects", "elspeth.web.sessions.models", "composer_continuation_effects_table"],
        ["events", "elspeth.web.sessions.models", "composer_continuation_events_table"],
        ["segments", "elspeth.web.sessions.models", "composer_operation_segments_table"],
        ["holds", "elspeth.web.sessions.models", "composer_provider_terminal_holds_table"],
        ["segment_attempts", "elspeth.web.sessions.models", "composer_segment_attempts_table"],
        ["physical", "elspeth.web.sessions.models", "quota_provider_attempts_table"],
        ["sessions_table", "elspeth.web.sessions.models", "sessions_table"],
        ["usage", "elspeth.web.sessions.models", "token_usage_ledger_table"],
        ["restore_utc", "elspeth.web.sessions.time_normalization", "restore_utc"],
    ],
}

V4_NO_SDK_GLOBALS = {
    "src/elspeth/web/composer/continuation_primary.py": [],
    "src/elspeth/web/composer/continuation_title_provider.py": [],
    "src/elspeth/web/sessions/continuation_classifier_primary.py": [
        [
            "_returned",
            "_returned: WeakKeyDictionary[\n    _PublishPermit, tuple[PreparedClassifierPrimary, StoredContinuationSnapshot, _CallModelOutcome, RecorderSnapshot]\n] = WeakKeyDictionary()",
        ]
    ],
    "src/elspeth/web/composer/continuation_primary_no_sdk.py": [],
    "src/elspeth/web/composer/continuation_classifier_no_sdk.py": [],
    "src/elspeth/web/composer/continuation_title_no_sdk.py": [],
    "src/elspeth/web/composer/provider_quota.py": [],
    "src/elspeth/web/sessions/service.py": [],
    "src/elspeth/web/required_work.py": [],
    "src/elspeth/web/coordination/composer_operation_authority.py": [],
    "src/elspeth/web/sessions/continuation_primary.py": [],
    "src/elspeth/web/sessions/continuation_primary_paid_overflow_refusal.py": [],
    "src/elspeth/web/sessions/continuation_primary_paid_overflow_refusal_terminal.py": [],
    "src/elspeth/web/sessions/continuation_title_no_sdk_close.py": [],
    "src/elspeth/web/sessions/continuation_title_no_sdk_terminal.py": [],
    "src/elspeth/web/sessions/continuation_classifier_primary_no_sdk.py": [
        ["_CODEC", '_CODEC = "composer.classifier-primary-no-sdk-refusal.v1"']
    ],
}

QUOTA = "elspeth.web.composer.provider_quota"
OWNER = QUOTA + ".ProviderInvocationOwner"
CUSTODY = QUOTA + ".ProviderCallCustody"
FAMILY = QUOTA + ".ProviderInvocationFamily"
BINDING = "elspeth.web.required_work.RequiredWorkBinding"
TITLE = "elspeth.web.composer.continuation_title_provider"
PRIMARY = "elspeth.web.composer.continuation_primary"
PRIMARY_PATH = "src/elspeth/web/composer/continuation_primary.py"
SOURCE_PATHS = frozenset({"src/" + key.split(":", 1)[0].replace(".", "/") + ".py" for key in CONTRACTS} | {PRIMARY_PATH})
DRIVER = "elspeth.web.sessions.continuation_driver"
DRIVER_CLASS_HEADER = """
class _SegmentDriver:
    def __init__(self, worker: ComposerAsyncWorker, services: ComposerAppServices, running: ComposerOperationRunning, lease: SessionOperationLease): pass
    async def start_progress(self, record: ComposerOperationRecord) -> None: pass
    async def close_progress(self, *, retain_failure: bool = False) -> None: pass
    def join_failure(self, error: BaseException, *additional: BaseException) -> BaseException: pass
    async def record(self) -> ComposerOperationRecord: pass
    async def selected(self) -> tuple[StoredContinuationSnapshot, SelectedContinuationState]: pass
    async def settle(self, record: ComposerOperationRecord, original: BaseException | None = None, *, expired_failure: ComposerOperationError | None = None) -> ContinuationSegmentSettled: pass
    async def settle_expired(self, original: BaseException | None, *, timeout_seconds: float) -> ContinuationSegmentSettled: pass
    async def read_failure(self, names: frozenset[str]) -> ContinuationToolFailureRead: pass
    async def retain_reply_deadline(self, observed: ObservedClassifierReplyDeadline, *, expiry: _ActionExpiry, stage: ContinuationStage[_ActionResult], clock: ContinuationActionClock) -> None: pass
    async def consume_tool_refusal(self, expected: ToolNoExecutionRefusal) -> ContinuationSegmentSettled: pass
    async def dispose(self, original: BaseException | None = None) -> ContinuationSegmentOutcome: pass
    async def advance(self, stored: StoredContinuationSnapshot, selected: SelectedContinuationState) -> _ActionResult: pass
    async def pending_control(self, selected: SelectedContinuationState) -> StoredContinuationControlIntent | None: pass
    async def iteration_stage(self, stored: StoredContinuationSnapshot, selected: SelectedContinuationState) -> IterationStageData | None: pass
    async def yield_control(self, stored: StoredContinuationSnapshot, selected: SelectedContinuationState, *, reason: Literal["model_request", "action_fit"], action_fit: ActionFitSuccessorPlan | None = None) -> ContinuationSegmentOutcome: pass
    async def watch_stage(self, stage: asyncio.Task[_ActionResult], reader: ComposerOperationWatchReader) -> None: pass
    def _worker_is_draining(self) -> bool: pass
    async def run(self, adoption_failure: BaseException | None) -> ContinuationSegmentOutcome: pass
"""


def _module(unit):
    if not unit.path.startswith("src/") or not unit.path.endswith(".py"):
        return None
    return unit.path[4:-3].replace("/", ".").removesuffix(".__init__")


def _node(unit, qualified):
    body = unit.tree.body
    for name in qualified.split("."):
        nodes = [n for n in body if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
        if len(nodes) != 1:
            return None
        node = nodes[0]
        body = node.body
    return node


def _shape(node):
    if isinstance(node, list):
        return tuple(_shape(n) for n in node)
    if not isinstance(node, ast.AST):
        return node
    fields = []
    for key, value in ast.iter_fields(node):
        if (
            (isinstance(node, ast.arg) and key == "annotation")
            or (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and key == "returns")
            or (isinstance(node, ast.AnnAssign) and key == "annotation")
            or key == "type_comment"
        ):
            continue
        if (
            key == "body"
            and isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and value
            and isinstance(value[0], ast.Expr)
            and isinstance(value[0].value, ast.Constant)
            and isinstance(value[0].value.value, str)
        ):
            value = value[1:]
        fields.append((key, _shape(value)))
    return type(node).__name__, tuple(fields)


def _imports(tree):
    env = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                env[a.asname or a.name.partition(".")[0]] = a.name if a.asname else a.name.partition(".")[0]
        if isinstance(node, ast.ImportFrom) and not node.level and node.module:
            for a in node.names:
                env[a.asname or a.name] = node.module + "." + a.name
    return env


def _qualify(node, env):
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, ast.Attribute):
        root = _qualify(node.value, env)
        return root + "." + node.attr if root else None
    return None


def _executed_nodes(node, caller=None):
    """Finite caller executable sites, separately from structural dependencies.

    Enter only the specified actual caller body. Nested function/lambda bodies
    have separate execution; their defaults and decorators execute here.
    Caller modules require postponed annotations, and local annotations do not
    execute. No annotation descendant may become an authorized transfer site.
    """
    yield node
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        for value in [
            *node.args.defaults,
            *(v for v in node.args.kw_defaults if v is not None),
            *(node.decorator_list if not isinstance(node, ast.Lambda) else []),
        ]:
            yield from _executed_nodes(value)
        if node is caller:
            for statement in node.body:
                yield from _executed_nodes(statement)
        return
    if isinstance(node, ast.AnnAssign):
        yield from _executed_nodes(node.target)
        if node.value is not None:
            yield from _executed_nodes(node.value)
        return
    if isinstance(node, ast.arg):
        return
    for child in ast.iter_child_nodes(node):
        yield from _executed_nodes(child)


def _call_universe(units, checked):
    """Direct qualified imports/constructor aliases; outside closure stays UNKNOWN."""
    owner_calls, mint_calls, escapes = set(), set(), []
    for unit in units:
        module = _module(unit)
        if module is None:
            continue
        initial = _imports(unit.tree)
        if module == QUOTA:
            initial.update({"ProviderInvocationOwner": OWNER, "ProviderCallCustody": CUSTODY, "ProviderInvocationFamily": FAMILY})
        parents = {id(c): n for n in ast.walk(unit.tree) for c in ast.iter_child_nodes(n)}
        postponed = any(
            isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
            for n in unit.tree.body
        )

        def scan(node, env, *, unit=unit, module=module, postponed=postponed, parents=parents):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                for value in [
                    *node.args.defaults,
                    *(v for v in node.args.kw_defaults if v is not None),
                    *(node.decorator_list if not isinstance(node, ast.Lambda) else []),
                ]:
                    scan(value, env)
                local = dict(env)
                for arg in [
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                    *([node.args.vararg] if node.args.vararg else []),
                    *([node.args.kwarg] if node.args.kwarg else []),
                ]:
                    local[arg.arg] = None
                if (unit.path, id(node)) in checked and (
                    node.name in {"_new_primary_custody", "_new_advisor_custody"}
                    or (module == PRIMARY and node.name == "run_continuation_primary")
                ):
                    local["provider_owner"] = OWNER + ".__instance__"
                if (unit.path, id(node)) in checked and module == TITLE and node.name == "run_prepared_title_provider":
                    # Only this complete checked entry validates its actual
                    # owner and retained work/service before the TITLE mint.
                    local["invocation_owner"] = OWNER + ".__instance__"
                for statement in [node.body] if isinstance(node, ast.Lambda) else node.body:
                    scan(statement, local)
                return
            if isinstance(node, ast.ClassDef):
                for value in [*node.bases, *node.decorator_list, *(k.value for k in node.keywords)]:
                    scan(value, env)
                for statement in node.body:
                    scan(statement, dict(env))
                return
            children = ast.iter_child_nodes(node)
            if isinstance(node, ast.AnnAssign) and postponed:
                children = iter([node.target, *([node.value] if node.value is not None else [])])
            for child in children:
                scan(child, env)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                wrapper = ast.Module(body=[node], type_ignores=[])
                env.update(_imports(wrapper))
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                origin = _qualify(value, env)
                if isinstance(value, ast.Call) and _qualify(value.func, env) == OWNER:
                    origin = OWNER + ".__instance__"
                for target in targets:
                    if isinstance(target, ast.Name):
                        env[target.id] = origin
            if isinstance(node, ast.Call):
                origin = _qualify(node.func, env)
                if origin == OWNER:
                    owner_calls.add((unit.path, id(node)))
                if origin == OWNER + ".__instance__.mint":
                    mint_calls.add((unit.path, id(node)))
            if (
                isinstance(node, (ast.Name, ast.Attribute))
                and isinstance(node.ctx, ast.Load)
                and _qualify(node, env) in {OWNER, OWNER + ".__instance__.mint"}
            ):
                parent = parents.get(id(node))
                # Nominal type guards are observations, not callable escapes.
                guard = isinstance(parent, ast.Compare) and any(
                    isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "type"
                    for c in [parent.left, *parent.comparators]
                )
                if (
                    not guard
                    and (not isinstance(parent, ast.Call) or parent.func is not node)
                    and not isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
                ):
                    # Postponed annotations are collected as dependencies;
                    # they cannot establish a runtime mint transfer.
                    escapes.append((unit.path, id(node)))

        for statement in unit.tree.body:
            scan(statement, dict(initial))
    return owner_calls, mint_calls, escapes


def provider_driver_constructor_contract(units):
    """One real driver field store, independent of other provider prerequisites."""
    drivers = [unit for unit in units if _module(unit) == DRIVER]
    if len(drivers) != 1:
        return ["provider driver source missing or duplicated"], None
    driver = drivers[0]
    cls = _node(driver, "_SegmentDriver")
    init = _node(driver, "_SegmentDriver.__init__")
    if (
        not isinstance(cls, ast.ClassDef)
        or not isinstance(init, ast.FunctionDef)
        or not owned_origin(driver.tree, "_SegmentDriver", cls)
        or not any(
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
            for node in driver.tree.body
        )
    ):
        return ["provider driver canonical own declaration changed"], None
    # Only this finite class's evaluated header is checked. Other method bodies
    # remain Common dependencies; no recursive callee or receiver permission.
    header = copy.copy(cls)
    header.body = []
    for method in cls.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return ["provider driver plain constructor namespace changed"], None
        item = copy.copy(method)
        item.body = [ast.Pass()]
        header.body.append(item)
    if _shape(header) != _shape(ast.parse(DRIVER_CLASS_HEADER).body[0]):
        return ["provider driver plain constructor namespace changed"], None
    expected = ast.parse(CONTRACTS[DRIVER + ":_SegmentDriver.__init__"]).body[0]
    if _shape(init) != _shape(expected):
        return ["provider driver complete constructor changed"], None
    expected_imports = {
        "ProviderInvocationOwner": OWNER,
        "RequiredWorkBinding": BINDING,
        "RequiredWorkRole": "elspeth.web.required_work.RequiredWorkRole",
        "RequiredWorkCoordinator": "elspeth.web.required_work.RequiredWorkCoordinator",
        "ComposerServiceImpl": "elspeth.web.composer.service.ComposerServiceImpl",
        "ContinuationLivePhases": "elspeth.web.composer.continuation_live_phases.ContinuationLivePhases",
        "SessionOperationLease": "elspeth.web.coordination.lifecycle.SessionOperationLease",
        "AuditIntegrityError": "elspeth.contracts.errors.AuditIntegrityError",
    }
    if any(
        import_origin(driver.tree, name, origin.rpartition(".")[0], origin.rpartition(".")[2]) is None
        for name, origin in expected_imports.items()
    ) or list(binding_nodes(scope_nodes(driver.tree), "type")):
        return ["provider driver canonical constructor supplier changed"], None
    store_matches = [
        node
        for node in init.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and ast.dump(node.targets[0], include_attributes=False)
        == ast.dump(ast.parse("self.provider = provider").body[0].targets[0], include_attributes=False)
    ]
    expected_store = ast.parse("self.provider = ProviderInvocationOwner(service=services.session_service, required_work=self.work)").body[0]
    forwarding = [
        node
        for node in init.body
        if isinstance(node, ast.Assign)
        and _shape(node)
        == _shape(ast.parse("self.phases = ContinuationLivePhases(self.service, self.authority, self.work, self.provider, lease)").body[0])
    ]
    work = [
        node
        for node in init.body
        if isinstance(node, ast.Assign)
        and _shape(node)
        == _shape(ast.parse("self.work = RequiredWorkBinding(lease.required_work, 0, 0, RequiredWorkRole.TURN, running)").body[0])
    ]
    if len(store_matches) != 1 or _shape(store_matches[0]) != _shape(expected_store) or len(forwarding) != 1 or len(work) != 1:
        return ["provider driver exact work/owner/phase transfer changed"], None
    store = store_matches[0]
    stores = (store, work[0], forwarding[0])
    targets = {id(statement.targets[0]) for statement in stores}
    if any(
        isinstance(node, ast.Attribute)
        and isinstance(node.ctx, (ast.Store, ast.Del))
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr in {"work", "provider", "phases"}
        and id(node) not in targets
        for node in ast.walk(cls)
    ):
        return ["provider driver retained work/owner/phase field rebound"], None
    return [], {
        "owner_constructor_stores": frozenset({(driver.path, id(store))}),
        "owner_constructor_calls": frozenset({(driver.path, id(store.value))}),
        "binding_constructor_stores": frozenset((driver.path, id(statement)) for statement in (work[0], forwarding[0])),
        "caller_regions": frozenset({(driver.path, id(init))}),
        "class_origins": frozenset({DRIVER + "._SegmentDriver"}),
    }


def continuation_primary_mint_contract(units):
    """The actual direct PRIMARY mint after its exact same-claim/work/service intake."""
    selected = [unit for unit in units if unit.path == PRIMARY_PATH]
    if len(selected) != 1:
        return ["provider continuation PRIMARY source missing or duplicated"], None
    unit = selected[0]
    caller = _node(unit, "run_continuation_primary")
    guard = _node(unit, "_require_owners")
    expected_guard = ast.parse(
        """
def _require_owners(authority: ComposerAsyncOperationAuthority, work: RequiredWorkBinding) -> None:
    if type(authority) is not ComposerAsyncOperationAuthority or type(work) is not RequiredWorkBinding or work.running is None:
        raise AuditIntegrityError("primary action requires actual current SQL and required-work owners")
    current = work.coordinator.authority
    if (current.authority_kind is not RequiredAuthorityKind.DURABLE_COMPOSE
        or current.durable_operation_id != work.running.claim.operation_id
        or current.claim_attempt != work.running.claim.attempt):
        raise AuditIntegrityError("primary action changed its actual durable required-work claim")
    work.validate_context(work.running.session_operation_context)
"""
    ).body[0]
    header = ast.parse(
        """
async def run_continuation_primary(
    *, service: ComposerServiceImpl, authority: ComposerAsyncOperationAuthority, selected: SelectedContinuationState,
    producers: CurrentContinuationMessageProducers, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
    recorder: BufferingRecorder, drained: RecorderDrainCursors, required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner, timeout: float, on_sdk_entry: Callable[[], None] | None = None,
) -> PrimaryProviderResult | PrimaryPaidOverflowTerminal | PrimaryPaidIncompleteTerminal | PrimarySdkTerminal | SdkTerminalFailure | PrimaryNoSdkRefusalReceipt:
    pass
"""
    ).body[0]
    prefix = ast.parse(
        """
_require_owners(authority, required_work)
running = required_work.running
if (
    running is None
    or type(service) is not ComposerServiceImpl
    or type(producers) is not CurrentContinuationMessageProducers
    or (
        type(provider_owner) is not ProviderInvocationOwner
        or provider_owner.required_work is not required_work
        or provider_owner.service is not service._require_sessions_service()
        or producers.inventory.running is not running
    )
):
    raise AuditIntegrityError("primary invocation crossed actual service/claim/provider owners")
if type(selected) is not SelectedContinuationState or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
    raise AuditIntegrityError("primary invocation requires its actual selected input and finite positive allowance")
"""
    ).body
    if (
        not isinstance(caller, ast.AsyncFunctionDef)
        or caller.decorator_list
        or caller.type_params
        or ast.dump(caller.args, include_attributes=False) != ast.dump(header.args, include_attributes=False)
        or ast.dump(caller.returns, include_attributes=False) != ast.dump(header.returns, include_attributes=False)
        or not any(
            isinstance(node, ast.ImportFrom) and node.module == "__future__" and any(alias.name == "annotations" for alias in node.names)
            for node in unit.tree.body
        )
        or _shape(caller.body[: len(prefix)]) != _shape(prefix)
        or guard is None
        or _shape(guard) != _shape(expected_guard)
        or not owned_origin(unit.tree, "run_continuation_primary", caller)
        or not owned_origin(unit.tree, "_require_owners", guard)
    ):
        return ["provider continuation PRIMARY exact nominal intake changed"], None
    imports = {
        "ProviderInvocationOwner": OWNER,
        "ProviderInvocationFamily": FAMILY,
        "RequiredWorkBinding": BINDING,
        "RequiredAuthorityKind": "elspeth.web.required_work.RequiredAuthorityKind",
        "ComposerAsyncOperationAuthority": "elspeth.web.coordination.composer_operation_authority.ComposerAsyncOperationAuthority",
        "ComposerServiceImpl": "elspeth.web.composer.service.ComposerServiceImpl",
        "CurrentContinuationMessageProducers": "elspeth.web.composer.continuation_prompt_producers.CurrentContinuationMessageProducers",
        "SelectedContinuationState": "elspeth.web.composer.continuation_selection.SelectedContinuationState",
        "AuditIntegrityError": "elspeth.contracts.errors.AuditIntegrityError",
        "ComposerContextUnsupported": "elspeth.web.composer.protocol.ComposerContextUnsupported",
        "ContinuationActionClock": "elspeth.web.composer.continuation_clock.ContinuationActionClock",
        "PrimaryNoSdkIssuer": "elspeth.web.composer.continuation_primary_no_sdk.PrimaryNoSdkIssuer",
    }
    if any(
        import_origin(unit.tree, binding, qualified.rpartition(".")[0], qualified.rpartition(".")[2]) is None
        for binding, qualified in imports.items()
    ) or any(list(binding_nodes(scope_nodes(unit.tree), binding)) for binding in ("type", "int", "float")):
        return ["provider continuation PRIMARY canonical intake supplier changed"], None
    math_imports = [
        alias
        for statement in unit.tree.body
        if isinstance(statement, ast.Import)
        for alias in statement.names
        if alias.name == "math" and alias.asname is None
    ]
    if len(math_imports) != 1 or list(binding_nodes(scope_nodes(unit.tree), "math")) != math_imports:
        return ["provider continuation PRIMARY canonical timing supplier changed"], None
    protected = {"provider_owner", "required_work", "service", "producers", "running", "type", *imports}
    if any(isinstance(node, (ast.Global, ast.Nonlocal)) and protected.intersection(node.names) for node in ast.walk(caller)):
        return ["provider continuation PRIMARY retained owner scope escaped"], None
    # Parameters and the one original local running store are the only binders.
    for binding in protected:
        bound = list(binding_nodes(ast.walk(caller), binding))
        allowed = [
            prefix_target
            for statement in caller.body[: len(prefix)]
            if isinstance(statement, ast.Assign)
            for prefix_target in statement.targets
            if isinstance(prefix_target, ast.Name) and prefix_target.id == binding
        ]
        if bound != allowed:
            return ["provider continuation PRIMARY retained owner rebound " + binding], None
    parents = {id(child): parent for parent in ast.walk(caller) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(caller):
        if not isinstance(node, ast.Name) or node.id != "provider_owner" or not isinstance(node.ctx, ast.Load):
            continue
        parent = parents.get(id(node))
        observed = isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and parent.func.id == "type" and parent.args == [node]
        member = (
            isinstance(parent, ast.Attribute)
            and parent.value is node
            and isinstance(parent.ctx, ast.Load)
            and parent.attr in {"required_work", "service", "cost_budget", "bind_cost_budget", "mint"}
        )
        if not (observed or member):
            return ["provider continuation PRIMARY owner escaped its checked member uses"], None
    expected_mint = ast.parse(
        "custody = provider_owner.mint(ProviderInvocationFamily.PRIMARY)\n"
        "custody.bind_continuation_primary_clock(clock)\n"
        "custody.bind_primary_no_sdk_issuer(issuer)"
    ).body
    candidates = [
        node
        for node in _executed_nodes(caller, caller=caller)
        if isinstance(node, ast.Call) and _qualify(node.func, {"provider_owner": OWNER + ".__instance__"}) == OWNER + ".__instance__.mint"
    ]
    if len(candidates) != 1:
        return ["provider continuation PRIMARY direct mint inventory changed"], None
    minted = candidates[0]
    statement = parents.get(id(minted))
    block = parents.get(id(statement))
    if (
        not isinstance(block, ast.Try)
        or block not in caller.body
        or _shape(block.body[:3]) != _shape(expected_mint)
        or block.body[0] is not statement
    ):
        return ["provider continuation PRIMARY exact mint/clock/issuer transfer changed"], None
    for binding, expected_source in (
        ("clock", "clock = ContinuationActionClock(policy.actions.primary)"),
        (
            "issuer",
            "issuer = PrimaryNoSdkIssuer(running=running, work=required_work, selected_checkpoint_id=selected.checkpoint_id, "
            "selected_cursor=selected.stack.cursor, input_sha256=invocation.request.input_sha256, clock=clock)",
        ),
    ):
        expected = ast.parse(expected_source).body[0]
        stores = [statement for statement in caller.body if _shape(statement) == _shape(expected)]
        if (
            len(stores) != 1
            or list(binding_nodes(ast.walk(caller), binding)) != [stores[0].targets[0]]
            or caller.body.index(stores[0]) >= caller.body.index(block)
            or any(isinstance(node, (ast.Global, ast.Nonlocal)) and binding in node.names for node in ast.walk(caller))
        ):
            return ["provider continuation PRIMARY original " + binding + " construction changed"], None
    return [], {"caller": caller, "guard": guard, "mint": minted, "unit": unit}


def current_provider_lifecycle_contract(units):
    """Keep current family/issuer/refusal checks independent of body-template pins."""
    selected = {module: [unit for unit in units if _module(unit) == module] for module in (QUOTA, TITLE)}
    if any(len(values) != 1 for values in selected.values()):
        return ["provider current lifecycle source missing or duplicated"]
    quota, title = selected[QUOTA][0], selected[TITLE][0]
    family = _node(quota, "ProviderInvocationFamily")
    expected_family = ast.parse("class ProviderInvocationFamily(IntEnum):\n    PRIMARY = 0\n    ADVISOR = 1\n    TITLE = 3").body[0]
    mint = _node(quota, "ProviderInvocationOwner.mint")
    guard = ast.parse(
        "if type(family) is not ProviderInvocationFamily:\n"
        "    raise AuditIntegrityError('Provider family is not an owned nominal identity')"
    ).body[0]
    expected_return = ast.parse(
        "return ProviderCallCustody(service=self.service, context=binding.coordinator.authority.context, required_work=binding, "
        "cost_budget=self.cost_budget if family in (ProviderInvocationFamily.PRIMARY, ProviderInvocationFamily.ADVISOR) else None, "
        "family=family)"
    ).body[0]
    if (
        family is None
        or _shape(family) != _shape(expected_family)
        or not owned_origin(quota.tree, "ProviderInvocationFamily", family)
        or import_origin(quota.tree, "IntEnum", "enum", "IntEnum") is None
        or not isinstance(mint, ast.FunctionDef)
        or _shape(mint.body[0]) != _shape(guard)
        or _shape(mint.body[-1]) != _shape(expected_return)
        or list(binding_nodes(ast.walk(mint), "family"))
        or any(isinstance(node, (ast.Global, ast.Nonlocal)) and "family" in node.names for node in ast.walk(mint))
    ):
        return ["provider current nominal family or exact custody forwarding changed"]
    ctor = _node(quota, "ProviderCallCustody.__init__")
    expected_prefix = ast.parse(
        """
if type(required_work) is not RequiredWorkBinding:
    raise AuditIntegrityError("Provider custody requires a nominal explicit binding")
required_work.validate_context(context)
if context.operation_kind is not SessionOperationKind.COMPOSE:
    raise AuditIntegrityError("Provider custody requires exact COMPOSE authority")
if cost_budget is not None and type(cost_budget) is not CommonProviderCostBudget:
    raise AuditIntegrityError("provider custody received foreign cost policy")
if family is not None and type(family) is not ProviderInvocationFamily:
    raise AuditIntegrityError("Provider custody family requires its owned nominal discriminator")
self._family = family
"""
    ).body
    if not isinstance(ctor, ast.FunctionDef) or _shape(ctor.body[: len(expected_prefix)]) != _shape(expected_prefix):
        return ["provider current custody nominal work/context/family changed"]
    custody = _node(quota, "ProviderCallCustody")
    for attribute, expression in (("_context", "context"), ("_required_work", "required_work"), ("_family", "family")):
        stores = [
            node
            for node in ast.walk(custody)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr == attribute
        ]
        expected = ast.parse("self." + attribute + " = " + expression).body[0]
        matches = [statement for statement in ctor.body if _shape(statement) == _shape(expected)]
        if len(matches) != 1 or stores != [matches[0].targets[0]]:
            return ["provider current custody protected origin rebound " + attribute]
    expected_methods = ast.parse(
        """
def bind_continuation_primary_clock(self, clock: ContinuationActionClock) -> None:
    from elspeth.web.composer.continuation_clock import ContinuationActionClock
    if (type(clock) is not ContinuationActionClock or self.continuation_title is not None
        or self.continuation_primary_clock is not None or self.attempt is not None
        or self.required_work.role is not RequiredWorkRole.TURN):
        raise AuditIntegrityError("PRIMARY timing requires its original pre-dispatch action owner")
    self.continuation_primary_clock = clock

def bind_primary_no_sdk_issuer(self, issuer: PrimaryNoSdkIssuer) -> None:
    from elspeth.web.composer.continuation_primary_no_sdk import PrimaryNoSdkIssuer
    if (type(issuer) is not PrimaryNoSdkIssuer or self._primary_no_sdk_issuer is not None
        or self.continuation_primary_clock is not issuer.clock or self.attempt is not None):
        raise AuditIntegrityError("PRIMARY no-SDK issuer requires original unentered physical custody")
    issuer.bind_custody(self)
    self._primary_no_sdk_issuer = issuer

@property
def family(self) -> ProviderInvocationFamily | None:
    return self._family

@property
def continuation_semantic_clock(self) -> ContinuationActionClock | None:
    issuer = self._semantic_no_sdk_issuer
    if issuer is None:
        return None
    if self.family is not ProviderInvocationFamily.ADVISOR:
        raise AuditIntegrityError("Semantic timing requires its nominal ADVISOR mint family")
    issuer.require_original_clock()
    return issuer.clock

def bind_semantic_no_sdk_issuer(self, issuer: SemanticAdvisorNoSdkIssuer) -> None:
    from elspeth.web.composer.continuation_semantic_no_sdk import SemanticAdvisorNoSdkIssuer
    if (type(issuer) is not SemanticAdvisorNoSdkIssuer or self.family is not ProviderInvocationFamily.ADVISOR
        or self._semantic_no_sdk_issuer is not None or self._primary_no_sdk_issuer is not None
        or self.continuation_primary_clock is not None or self.continuation_title is not None
        or self.continuation_semantic_clock is not None or self.attempt is not None):
        raise AuditIntegrityError("Semantic timing and no-SDK authority require original ADVISOR custody")
    issuer.bind_custody(self)
    self._semantic_no_sdk_issuer = issuer
"""
    ).body
    for expected in expected_methods:
        actual = _node(quota, "ProviderCallCustody." + expected.name)
        if actual is None or _shape(actual) != _shape(expected):
            return ["provider current issuer/clock custody guard changed " + expected.name]
    cancel = _node(quota, "ProviderCallCustody._cancel_undispatched")
    admit = _node(quota, "ProviderCallCustody.admit")
    entered = _node(quota, "ProviderCallCustody.mark_sdk_entered")
    cancel_prefix = ast.parse(
        "if self.attempt is None or self._sdk_entered:\n"
        "    raise AuditIntegrityError('Undispatched disposition lacks exact no-SDK attempt evidence')\n"
        "admitted = self.attempt"
    ).body
    cancel_hooks = ast.parse(
        "if self._primary_no_sdk_issuer is not None:\n"
        "    self._primary_no_sdk_issuer.note_undispatched_cancel(self, admitted, model=model)\n"
        "if self._semantic_no_sdk_issuer is not None:\n"
        "    self._semantic_no_sdk_issuer.note_undispatched_cancel(self, admitted, model=model)"
    ).body
    admission = ast.parse(
        "if type(attempt) is not ProviderAttempt:\n"
        "    raise AuditIntegrityError('Provider admission returned a foreign attempt')\n"
        "self.attempt = attempt\n"
        "if self._primary_no_sdk_issuer is not None:\n"
        "    self._primary_no_sdk_issuer.note_admitted_attempt(self, attempt, model=model)\n"
        "if self._semantic_no_sdk_issuer is not None:\n"
        "    self._semantic_no_sdk_issuer.note_admitted_attempt(self, attempt, model=model)"
    ).body
    sdk_prefix = ast.parse(
        "if self.attempt is None or self._sdk_entered or self.call is not None:\n"
        "    raise AuditIntegrityError('Physical provider dispatch lacks exact admitted custody')\n"
        "if self._primary_no_sdk_issuer is not None:\n"
        "    self._primary_no_sdk_issuer.mark_sdk_entered(self)\n"
        "if self._semantic_no_sdk_issuer is not None:\n"
        "    self._semantic_no_sdk_issuer.mark_sdk_entered(self)"
    ).body
    if (
        not isinstance(cancel, ast.AsyncFunctionDef)
        or _shape(cancel.body[:2]) != _shape(cancel_prefix)
        or _shape(cancel.body[6:8]) != _shape(cancel_hooks)
        or not isinstance(admit, ast.AsyncFunctionDef)
        or sum(isinstance(node, ast.Try) and _shape(node.body[:4]) == _shape(admission) for node in ast.walk(admit)) != 1
        or not isinstance(entered, ast.FunctionDef)
        or _shape(entered.body[:3]) != _shape(sdk_prefix)
    ):
        return ["provider current issuer admission/cancel/SDK event transfer changed"]
    title_entry = _node(title, "run_prepared_title_provider")
    title_prefix = ast.parse(
        """
if type(prepared) is not PreparedTitleProvider or type(invocation_owner) is not ProviderInvocationOwner:
    raise AuditIntegrityError("TITLE execution requires its actual prepared/current invocation owners")
if invocation_owner.required_work is not prepared.work or invocation_owner.service is not _sessions(prepared.composer):
    raise AuditIntegrityError("TITLE execution changed its actual original work/service")
root = decode_continuation_snapshot(prepared.selected.payload_json)
decode_continuation_policy(root.admission_policy_json, root.policy_hash)
custody = invocation_owner.mint(ProviderInvocationFamily.TITLE)
custody.bind_continuation_title(prepared)
"""
    ).body
    if not isinstance(title_entry, ast.AsyncFunctionDef) or _shape(title_entry.body[: len(title_prefix)]) != _shape(title_prefix):
        return ["provider current TITLE policy validation must precede exact mint"]
    for binding, module in (
        ("decode_continuation_policy", "elspeth.web.composer.continuation_policy"),
        ("decode_continuation_snapshot", "elspeth.web.composer.continuation_codec"),
        ("_sessions", "elspeth.web.composer.continuation_initialization"),
        ("PreparedTitleProvider", "elspeth.web.sessions.continuation_title_provider"),
    ):
        if import_origin(title.tree, binding, module, binding) is None:
            return ["provider current TITLE supplier changed " + binding]
    for binding, module in (
        ("RequiredWorkBinding", "elspeth.web.required_work"),
        ("RequiredWorkRole", "elspeth.web.required_work"),
        ("SessionOperationKind", "elspeth.contracts.session_operation"),
        ("CommonProviderCostBudget", "elspeth.web.composer.provider_cost"),
        ("ProviderAttempt", "elspeth.web.coordination.quota_authority"),
        ("AuditIntegrityError", "elspeth.contracts.errors"),
    ):
        if import_origin(quota.tree, binding, module, binding) is None:
            return ["provider current custody supplier changed " + binding]
    if any(list(binding_nodes(scope_nodes(unit.tree), binding)) for unit in (quota, title) for binding in ("type", "property")):
        return ["provider current lifecycle builtin supplier changed"]
    return []


def _v4_no_sdk_patterns(nodes):
    """Independent identity/order anchors, even if a complete template is rebound."""
    primary = "src/elspeth/web/composer/continuation_primary.py:"
    title = "src/elspeth/web/composer/continuation_title_provider.py:"
    classifier = "src/elspeth/web/sessions/continuation_classifier_primary.py:"
    for key in (
        primary + "_finish_v4_primary_no_sdk",
        title + "_close_v4_title_no_sdk",
        classifier + "ClassifierPrimaryIssuer._finish_v4_no_sdk",
    ):
        function = nodes[key]
        permitted = set()
        if key.startswith(classifier):
            tail = function.body[-2]
            if isinstance(tail, ast.Assign) and len(tail.targets) == 1 and ast.unparse(tail.targets[0]) == "self._terminal":
                permitted.add(id(tail.targets[0]))
        if any(
            isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)) and id(node) not in permitted
            for node in ast.walk(function)
        ):
            return ["V4 no-SDK producer changed retained custody before cold verification " + key]
    patterns = {
        primary + "_finish_v4_primary_no_sdk": (
            "issuer.reserve_v4_refusal(admission=admission, reason=reason, original_failure=original_failure)",
            "sessions.cancel_v4_continuation_primary_unentered(prepared=permit.prepared, admission=admission, permit=permit, required_work=ticket)",
            "required_work.coordinator.validate_undispatched_cancellation_sql_outcome(ticket=ticket, actual_outcome=outcome)",
            "proved is None or proved != receipt",
            "issuer.mark_refusal_committed(permit)",
            "custody.note_v4_primary_no_sdk(proved, admission)",
        ),
        title + "_close_v4_title_no_sdk": (
            "custody.require_v4_title_unentered(admission)",
            "issuer.note_original_failure(custody, error)",
            "issuer.reserve_refusal(reason=reason, original_failure=error)",
            "_sessions(prepared.composer).cancel_v4_continuation_title_unentered(permit=permit, required_work=ticket)",
            "prepared.work.coordinator.validate_undispatched_cancellation_sql_outcome(ticket=ticket, actual_outcome=outcome)",
            "verified.source != admission.hold",
            "verified.provider_attempt_id != admission.attempt.attempt_id",
            'verified.hold_state != "consumed"',
            "verified.reason is not reason",
        ),
        classifier + "ClassifierPrimaryIssuer._finish_v4_no_sdk": (
            "issuer.reserve_v4_refusal(admission=admission, reason=reason, original_failure=original_failure)",
            "sessions.cancel_v4_continuation_classifier_unentered(permit=permit, required_work=ticket)",
            "self.work.coordinator.validate_continuation_sql_outcome(ticket=ticket, expected_source=RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL, actual_outcome=outcome)",
            "receipt is not None and proved != receipt",
            "issuer.mark_refusal_committed(permit)",
            "custody.note_v4_classifier_no_sdk(receipt, admission)",
        ),
        "src/elspeth/web/composer/continuation_primary_no_sdk.py:PrimaryNoSdkIssuer": (
            "self._attempt_id != admission.attempt.attempt_id",
            "self._custody.require_v4_primary_unentered(admission)",
        ),
        "src/elspeth/web/composer/continuation_classifier_no_sdk.py:ClassifierNoSdkIssuer": (
            "self._attempt is not admission.attempt",
            "self._dispatched is not admission.dispatched",
            "self._custody.require_v4_classifier_unentered(admission)",
        ),
        "src/elspeth/web/composer/continuation_title_no_sdk.py:TitleNoSdkIssuer": (
            "original_failure is not self._failure",
            "permit.admission is not self._admission",
        ),
        "src/elspeth/web/sessions/continuation_primary_paid_overflow_refusal.py:refuse_v4_primary_unentered_on_connection": (
            "permit.undispatched_attempt_id != admission.attempt.attempt_id",
            "permit.issuer.require_refusal_permit(permit)",
            "changed_hold.rowcount != 1",
        ),
        "src/elspeth/web/sessions/continuation_title_no_sdk_close.py:_refuse_v4_title_no_sdk_locked_on_connection": (
            "child.provider_attempt_id != admission.attempt.attempt_id",
            "permit.issuer.require_refusal_permit(permit)",
            'verified.hold_state != "active"',
            'post.hold_state != "consumed"',
            "post.terminal != verified.terminal",
            "consumed.rowcount != 1",
        ),
        "src/elspeth/web/sessions/continuation_classifier_primary_no_sdk.py:refuse_classifier_no_sdk_on_connection": (
            "prepared.binding.running is not permit.issuer.running",
            "permit.issuer.require_refusal_permit(permit)",
            "changed_hold.rowcount != 1",
        ),
    }
    for key, required in patterns.items():
        actual = nodes[key]
        shapes = [_shape(node) for node in ast.walk(actual)]
        for source in required:
            expected = ast.parse(source, mode="eval").body
            if shapes.count(_shape(expected)) != 1:
                return ["V4 no-SDK original identity/outcome/terminal changed " + key]
    for key, expected_tail in (
        (
            primary + "_finish_v4_primary_no_sdk",
            """
issuer.mark_refusal_committed(permit)
custody.note_v4_primary_no_sdk(proved, admission)
return replace(proved, deferred_cancellations=tuple(cancellations))
""",
        ),
        (
            classifier + "ClassifierPrimaryIssuer._finish_v4_no_sdk",
            """
issuer.mark_refusal_committed(permit)
custody.note_v4_classifier_no_sdk(receipt, admission)
terminal = replace(receipt, deferred_cancellations=tuple(cancellations))
self._terminal = terminal
return terminal
""",
        ),
    ):
        tail = ast.parse(expected_tail).body
        if _shape(nodes[key].body[-len(tail) :]) != _shape(tail):
            return ["V4 no-SDK custody closed before original cold verification " + key]
    service = "src/elspeth/web/sessions/service.py:SessionServiceImpl.cancel_v4_continuation_"
    for family in ("primary", "title", "classifier"):
        key = service + family + "_unentered"
        method = nodes[key]
        required = ast.parse(
            "_validate_required_sql_ticket(required_work, sources=(RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL,), "
            "session_id=context.fence.session_id, context=context)",
            mode="eval",
        ).body
        if sum(_shape(n) == _shape(required) for n in ast.walk(method)) != 1:
            return ["V4 no-SDK original service SQL authority changed " + key]
        expected_return = ast.parse("return await run_required_sql_finish_once(required_work, _sync)").body[0]
        if _shape(method.body[-1]) != _shape(expected_return):
            return ["V4 no-SDK service lost its original finish-once writer " + key]
    for family, key in (
        (
            "primary",
            "src/elspeth/web/sessions/continuation_primary_paid_overflow_refusal_terminal.py:read_v4_primary_no_sdk_refusal_on_connection",
        ),
        ("classifier", "src/elspeth/web/sessions/continuation_classifier_primary_no_sdk.py:read_classifier_no_sdk_refusal_on_connection"),
    ):
        shapes = [_shape(node) for node in ast.walk(nodes[key])]
        for text in (
            'hold.state != "consumed"',
            "effect.provider_attempt_id != hold.provider_attempt_id",
            "parent.journal_cursor != terminal.cursor",
            "parent.checkpoint_id != terminal.checkpoint_id",
            "effect.response_finished_at is not None",
            "value != 0",
        ):
            if _shape(ast.parse(text, mode="eval").body) not in shapes:
                return ["V4 no-SDK cold original terminal/effect/H changed " + family]
    title_cold = nodes["src/elspeth/web/sessions/continuation_title_no_sdk_terminal.py:read_v4_title_no_sdk_terminal_on_connection"]
    title_shapes = [_shape(node) for node in ast.walk(title_cold)]
    for text in (
        "hold.provider_attempt_id != effect.provider_attempt_id",
        "parent.journal_cursor != terminal.cursor",
        "parent.checkpoint_id != terminal.checkpoint_id",
        "effect.response_finished_at is not None",
        "value != 0",
    ):
        if _shape(ast.parse(text, mode="eval").body) not in title_shapes:
            return ["V4 no-SDK cold original terminal/effect/H changed title"]
    # The outcome validation immediately consumes the carrier returned by this
    # writer, before any projection/custody closure in its containing block.
    for key, validator in (
        (primary + "_finish_v4_primary_no_sdk", "required_work.coordinator.validate_undispatched_cancellation_sql_outcome"),
        (title + "_close_v4_title_no_sdk", "prepared.work.coordinator.validate_undispatched_cancellation_sql_outcome"),
        (classifier + "ClassifierPrimaryIssuer._finish_v4_no_sdk", "self.work.coordinator.validate_continuation_sql_outcome"),
    ):
        blocks = [
            value
            for parent in ast.walk(nodes[key])
            for _name, value in ast.iter_fields(parent)
            if isinstance(value, list)
            and any(
                isinstance(n, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "outcome" for target in n.targets)
                for n in value
            )
        ]
        if len(blocks) != 1:
            return ["V4 no-SDK SQL outcome block changed " + key]
        block = blocks[0]
        position = next(
            i
            for i, n in enumerate(block)
            if isinstance(n, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "outcome" for target in n.targets)
        )
        following = block[position + 1] if position + 1 < len(block) else None
        if not (
            isinstance(following, ast.Expr) and isinstance(following.value, ast.Call) and ast.unparse(following.value.func) == validator
        ):
            return ["V4 no-SDK original SQL carrier validation moved " + key]
    return []


def v4_no_sdk_reserve_local_contracts(units):
    """Exact three cancellation reserves, conditional on current Common closure."""
    units = tuple(units)
    index = {unit.path: unit for unit in units}
    paths = {key.split(":", 1)[0] for key in V4_NO_SDK_SOURCE_CONTRACTS}
    if len(index) != len(units) or not paths <= index.keys():
        return ["V4 no-SDK source set is missing or duplicated"], None
    nodes = {}
    origins = set()
    owner_origins = set()
    source_nodes = set()
    for key in V4_NO_SDK_SOURCE_CONTRACTS:
        path, symbol = key.split(":", 1)
        unit = index[path]
        actual = _node(unit, symbol)
        if actual is None:
            return ["V4 no-SDK current declaration missing " + key], None
        owner_name = symbol.split(".", 1)[0]
        owner = _node(unit, owner_name)
        if not owned_origin(unit.tree, owner_name, owner):
            return ["V4 no-SDK canonical declaration rebound " + key], None
        if "." in symbol:
            final_authority = owner_name == "ComposerAsyncOperationAuthority"
            decorators = ast.parse("@final\nclass Owner: pass").body[0].decorator_list if final_authority else []
            if (
                not isinstance(owner, ast.ClassDef)
                or owner.bases
                or owner.keywords
                or _shape(owner.decorator_list) != _shape(decorators)
                or any(
                    isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and member.name in {"__getattribute__", "__getattr__", "__setattr__", "__delattr__", "__get__", "__set__", "__delete__"}
                    for member in owner.body
                )
                or (final_authority and import_origin(unit.tree, "final", "typing", "final") is None)
            ):
                return ["V4 no-SDK original owner lookup/header changed " + key], None
            owner_origins.add(_module(unit) + "." + owner_name)
        nodes[key] = actual
        origins.add(_module(unit) + "." + symbol)
        source_nodes.add((path, id(actual)))
    failures = _v4_no_sdk_patterns(nodes)
    if failures:
        return failures, None
    for key, source in V4_NO_SDK_SOURCE_CONTRACTS.items():
        if _shape(nodes[key]) != _shape(ast.parse(source).body[0]):
            return ["V4 no-SDK complete issuer/writer/cold contract changed " + key], None
    imports = set()
    primitives = {
        "type",
        "isinstance",
        "range",
        "all",
        "any",
        "len",
        "min",
        "max",
        "bool",
        "int",
        "float",
        "str",
        "tuple",
        "list",
        "set",
        "frozenset",
        "BaseException",
        "BaseExceptionGroup",
        "TimeoutError",
        "TypeError",
        "ValueError",
        "enumerate",
        "zip",
        "dict",
        "bytes",
        "property",
    }
    for path, bindings in V4_NO_SDK_IMPORTS.items():
        unit = index[path]
        for binding, source in V4_NO_SDK_GLOBALS[path]:
            expected = ast.parse(source).body[0]
            matches = [statement for statement in unit.tree.body if _shape(statement) == _shape(expected)]
            if len(matches) != 1:
                return ["V4 no-SDK original codec/owner cache binding changed " + path + ":" + binding], None
            statement = matches[0]
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if list(binding_nodes(scope_nodes(unit.tree), binding)) != targets:
                return ["V4 no-SDK codec/owner cache origin rebound " + path + ":" + binding], None
            source_nodes.add((path, id(statement)))
        for binding, module, member in bindings:
            if member is not None:
                fact = import_origin(unit.tree, binding, module, member)
            else:
                matches = [
                    (statement, alias)
                    for statement in unit.tree.body
                    if isinstance(statement, ast.Import)
                    for alias in statement.names
                    if alias.name == module and (alias.asname or alias.name.split(".")[0]) == binding
                ]
                fact = matches[0] if len(matches) == 1 and list(binding_nodes(scope_nodes(unit.tree), binding)) == [matches[0][1]] else None
            if fact is None:
                return ["V4 no-SDK canonical imported supplier changed " + path + ":" + binding], None
            imports.add((path, id(fact[1])))
        names = {
            n.id
            for key, node in nodes.items()
            if key.startswith(path + ":")
            for n in ast.walk(node)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        if any(list(binding_nodes(scope_nodes(unit.tree), name)) for name in primitives & names):
            return ["V4 no-SDK primitive supplier rebound " + path], None
        if any(
            list(binding_nodes(ast.walk(node), name))
            for key, node in nodes.items()
            if key.startswith(path + ":")
            for name in primitives & names
        ):
            return ["V4 no-SDK primitive supplier shadowed in original flow " + path], None
        if any(
            (isinstance(part, ast.arg) and part.arg in primitives)
            or (isinstance(part, (ast.Global, ast.Nonlocal)) and bool(primitives.intersection(part.names)))
            for key, node in nodes.items()
            if key.startswith(path + ":")
            for part in ast.walk(node)
        ):
            return ["V4 no-SDK primitive supplier escaped its original scope " + path], None
    producers = (
        (
            "src/elspeth/web/composer/continuation_primary.py",
            "_finish_v4_primary_no_sdk",
            "_run_v4_primary_after_prepared",
            "required_work",
        ),
        (
            "src/elspeth/web/composer/continuation_title_provider.py",
            "_close_v4_title_no_sdk",
            "run_prepared_title_provider",
            "prepared.work",
        ),
        (
            "src/elspeth/web/sessions/continuation_classifier_primary.py",
            "ClassifierPrimaryIssuer._finish_v4_no_sdk",
            "ClassifierPrimaryIssuer._invoke_v4_prepared",
            "self.work",
        ),
    )
    reserve_calls = set()
    caller_calls = set()
    allowed_references = set()
    terminal_calls = set()
    for path, method, caller, work in producers:
        function = nodes[path + ":" + method]
        expected = ast.parse(
            work + ".coordinator.reserve(RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL, "
            "transition_ordinal="
            + work
            + ".transition_ordinal, semantic_ordinal="
            + work
            + ".semantic_ordinal"
            + (
                ", recurrence_ordinal=attempt"
                if method == "_finish_v4_primary_no_sdk"
                else ", recurrence_ordinal=recurrence"
                if method.endswith("._finish_v4_no_sdk")
                else ""
            )
            + ")",
            mode="eval",
        ).body
        calls = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "reserve"
        ]
        if len(calls) != 1 or _shape(calls[0]) != _shape(expected):
            return ["V4 no-SDK exact original work/source/ordinals changed " + path], None
        reserve_calls.add((path, id(calls[0])))
        member = method.split(".")[-1]
        actual_calls = [
            node
            for node in ast.walk(nodes[path + ":" + caller])
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == member)
                or (isinstance(node.func, ast.Attribute) and node.func.attr == member)
            )
        ]
        if len(actual_calls) != (2 if member == "_close_v4_title_no_sdk" else 1):
            return ["V4 no-SDK original caller inventory changed " + path], None
        caller_calls.update((path, id(call)) for call in actual_calls)
        allowed_references.update((path, id(call.func)) for call in actual_calls)
        terminal_calls.update(
            (path, id(n))
            for n in ast.walk(function)
            if isinstance(n, ast.Call)
            and ast.unparse(n.func)
            in {"issuer.mark_refusal_committed", "custody.note_v4_primary_no_sdk", "custody.note_v4_classifier_no_sdk"}
        )
    names = {name.split(".")[-1] for _path, name, _caller, _work in producers}
    actual_references = {
        (unit.path, id(node))
        for unit in units
        for node in ast.walk(unit.tree)
        if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names)
        or (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and node.attr in names)
    }
    if actual_references != allowed_references:
        return ["V4 no-SDK original producer reference universe changed"], None
    return [], {
        "reserve_calls": frozenset(reserve_calls),
        "caller_calls": frozenset(caller_calls),
        "terminal_calls": frozenset(terminal_calls),
        "source_nodes": frozenset(source_nodes),
        "import_nodes": frozenset(imports),
        "protected_origins": frozenset(origins | owner_origins),
        "owner_origins": frozenset(owner_origins),
        "class_origins": frozenset(
            key.split(":", 1)[0].removeprefix("src/").removesuffix(".py").replace("/", ".") + "." + key.split(":", 1)[1]
            for key, node in nodes.items()
            if isinstance(node, ast.ClassDef)
        ),
        "function_origins": frozenset(
            key.split(":", 1)[0].removeprefix("src/").removesuffix(".py").replace("/", ".") + "." + key.split(":", 1)[1]
            for key, node in nodes.items()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ),
        "remaining_premises": (
            "COMMON_ORIGIN_RECEIVER_DESCRIPTOR_EFFECT_CLOSURE",
            "CURRENT_FROZEN_SOURCE_BINDINGS",
            "ORIGINAL_SQL_AND_COLD_READER_BEHAVIOR",
        ),
    }


def provider_mint_local_contracts(units):
    units = tuple(units)
    driver_failures, driver = provider_driver_constructor_contract(units)
    if driver_failures:
        return driver_failures, None
    primary_failures, primary = continuation_primary_mint_contract(units)
    if primary_failures:
        return primary_failures, None
    lifecycle_failures = current_provider_lifecycle_contract(units)
    if lifecycle_failures:
        return lifecycle_failures, None
    modules = {}
    for unit in units:
        module = _module(unit)
        if module is None:
            continue
        if module in modules:
            return ["provider mint source module duplicated " + module], None
        modules[module] = unit
    regions = []
    for qualified, source in CONTRACTS.items():
        module, name = qualified.split(":", 1)
        if module not in modules:
            return ["provider mint producer/caller source missing " + module], None
        unit = modules[module]
        if not any(
            isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
            for n in unit.tree.body
        ):
            return ["provider mint postponed definition contract changed " + module], None
        actual = _node(unit, name)
        expected = ast.parse(source).body[0]
        if actual is None or _shape(actual) != _shape(expected):
            return ["provider mint complete producer/retention/caller contract changed " + qualified], None
        regions.append((unit, name, actual))
    quota = modules[QUOTA]
    owner = _node(quota, "ProviderInvocationOwner")
    custody = _node(quota, "ProviderCallCustody")
    if (
        owner is None
        or custody is None
        or owner.bases
        or owner.keywords
        or owner.decorator_list
        or owner.type_params
        or custody.bases
        or custody.keywords
        or custody.decorator_list
        or custody.type_params
    ):
        return ["provider mint owned constructor/class lookup changed"], None
    hooks = {"__new__", "__getattribute__", "__getattr__", "__setattr__", "__delattr__", "__init_subclass__"}
    if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in hooks for n in custody.body):
        return ["provider mint custody lookup hook changed"], None
    expected_imports = {
        QUOTA: {
            "Any": "typing.Any",
            "AuditIntegrityError": "elspeth.contracts.errors.AuditIntegrityError",
            "Awaitable": "collections.abc.Awaitable",
            "Callable": "collections.abc.Callable",
            "ChargeableAdmissionRefused": "elspeth.contracts.chargeable_admission.ChargeableAdmissionRefused",
            "CommonProviderCostBudget": "elspeth.web.composer.provider_cost.CommonProviderCostBudget",
            "CommonProviderCostRequest": "elspeth.web.composer.provider_cost.CommonProviderCostRequest",
            "ComposerLLMCall": "elspeth.contracts.composer_llm_audit.ComposerLLMCall",
            "ComposerLLMCallStatus": "elspeth.contracts.composer_llm_audit.ComposerLLMCallStatus",
            "ComposerOwnedSettlementFailure": "elspeth.contracts.errors.ComposerOwnedSettlementFailure",
            "Coroutine": "collections.abc.Coroutine",
            "ProviderAttempt": "elspeth.web.coordination.quota_authority.ProviderAttempt",
            "RequiredWorkBinding": BINDING,
            "RequiredWorkRole": "elspeth.web.required_work.RequiredWorkRole",
            "RequiredWorkSource": "elspeth.web.required_work.RequiredWorkSource",
            "RequiredWorkTicket": "elspeth.web.required_work.RequiredWorkTicket",
            "RequiredSQLFinishOnce": "elspeth.web.required_sql_outcomes.RequiredSQLFinishOnce",
            "RequiredSQLRaised": "elspeth.web.required_sql_outcomes.RequiredSQLRaised",
            "RequiredSQLReturned": "elspeth.web.required_sql_outcomes.RequiredSQLReturned",
            "SQLAlchemyError": "sqlalchemy.exc.SQLAlchemyError",
            "SessionOperationContext": "elspeth.contracts.session_operation.SessionOperationContext",
            "SessionOperationFenceLost": "elspeth.web.coordination.contracts.SessionOperationFenceLost",
            "SessionOperationKind": "elspeth.contracts.session_operation.SessionOperationKind",
            "asyncio": "asyncio",
            "cast": "typing.cast",
            "contract_errors": "elspeth.contracts.errors",
            "time": "time",
            "replace": "dataclasses.replace",
            "timedelta": "datetime.timedelta",
        },
        "elspeth.web.composer.service": {"ProviderInvocationOwner": OWNER, "ProviderInvocationFamily": FAMILY},
        "elspeth.web.composer.advisor_checkpoint": {"ProviderInvocationOwner": OWNER, "ProviderInvocationFamily": FAMILY},
        TITLE: {"ProviderInvocationOwner": OWNER, "ProviderInvocationFamily": FAMILY},
        "elspeth.web.execution.routes": {
            "ProviderInvocationOwner": OWNER,
            "ProviderInvocationFamily": FAMILY,
            "RequiredWorkBinding": BINDING,
            "RequiredWorkRole": "elspeth.web.required_work.RequiredWorkRole",
            "RequiredWorkCoordinator": "elspeth.web.required_work.RequiredWorkCoordinator",
            "RequiredWorkAuthority": "elspeth.web.required_work.RequiredWorkAuthority",
            "RequiredAuthorityKind": "elspeth.web.required_work.RequiredAuthorityKind",
            "SessionOperationLease": "elspeth.web.coordination.lifecycle.SessionOperationLease",
            "SessionOperationKind": "elspeth.web.coordination.contracts.SessionOperationKind",
        },
    }
    for module, expected in expected_imports.items():
        imports = _imports(modules[module].tree)
        if any(imports.get(name) != origin for name, origin in expected.items()):
            return ["provider mint selected constructor/family/binding import origin changed " + module], None
    init = _node(quota, "ProviderInvocationOwner.__init__")
    mint = _node(quota, "ProviderInvocationOwner.mint")
    returned = mint.body[-1]
    if not isinstance(returned, ast.Return) or not isinstance(returned.value, ast.Call):
        return ["provider mint exact returned custody constructor changed"], None
    caller_regions = [
        (unit, node)
        for unit, name, node in regions
        if name
        in {
            "ComposerServiceImpl._new_primary_custody",
            "AdvisorCheckpointOwner._new_advisor_custody",
            "create_execution_router.evaluate_run_diagnostics",
            "run_prepared_title_provider",
            "_SegmentDriver.__init__",
        }
    ]
    caller_regions.append((primary["unit"], primary["caller"]))
    regions.extend(
        ((primary["unit"], "run_continuation_primary", primary["caller"]), (primary["unit"], "_require_owners", primary["guard"]))
    )
    constructor_sites = {
        (u.path, id(n))
        for u, f in caller_regions
        for n in _executed_nodes(f, caller=f)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "ProviderInvocationOwner"
    }
    mint_sites = {
        (u.path, id(n))
        for u, f in caller_regions
        for n in _executed_nodes(f, caller=f)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "mint"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id
        == (
            "owner"
            if _module(u) == "elspeth.web.execution.routes" and f.name == "evaluate_run_diagnostics"
            else "invocation_owner"
            if _module(u) == TITLE and f.name == "run_prepared_title_provider"
            else "provider_owner"
        )
    }
    found_constructors, found_mints, escapes = _call_universe(units, frozenset((u.path, id(f)) for u, f in caller_regions))
    if constructor_sites != found_constructors or mint_sites != found_mints:
        return ["provider mint actual qualified constructor/mint call universe changed"], None
    # This receipt is finite local source evidence. Every actual dependency and
    # unknown outside effect must remain a separately qualified common premise.
    dependencies = tuple(
        {
            "unit_path": u.path,
            "region": name,
            "node_id": id(n),
            "operator_node_id": id(n.func),
            "operator_ast": ast.dump(n.func, include_attributes=False),
            "qualification": "COMMON_ORIGIN_RECEIVER_EFFECT_PROOF_REQUIRED",
            "execution_role": "UNCLASSIFIED_COMMON_EXECUTION_ROLE_REQUIRED",
        }
        for u, name, region in regions
        for n in ast.walk(region)
        if isinstance(n, ast.Call)
    )
    receiver_dependencies = tuple(
        {
            "unit_path": u.path,
            "region": name,
            "node_id": id(n),
            "member_ast": ast.dump(n, include_attributes=False),
            "qualification": "COMMON_SOURCE_RECEIVER_MEMBER_EFFECT_PROOF_REQUIRED",
        }
        for u, name, region in regions
        for n in ast.walk(region)
        if isinstance(n, ast.Attribute)
    )

    def key(n):
        return quota.path, id(n)

    return [], {
        "owner_init": key(init),
        "mint_method": key(mint),
        "mint_return": key(returned),
        "custody_constructor_call": key(returned.value),
        "owner_constructor_calls": frozenset(constructor_sites),
        "owner_constructor_stores": driver["owner_constructor_stores"],
        "mint_calls": frozenset(mint_sites),
        "validated_transfer_methods": frozenset({key(mint)}),
        "caller_regions": frozenset((u.path, id(f)) for u, f in caller_regions),
        "dependencies": dependencies,
        "receiver_dependencies": receiver_dependencies,
        "dependency_classes": (
            OWNER,
            CUSTODY,
            FAMILY,
            BINDING,
            "elspeth.web.required_work.RequiredWorkRole",
            "elspeth.web.required_work.RequiredWorkCoordinator",
            "elspeth.web.required_work.RequiredWorkAuthority",
            DRIVER + "._SegmentDriver",
        ),
        "remaining_premises": (
            "UNKNOWN_COMMON_SOURCE_IMPORT_BINDING_CLOSURE",
            "UNKNOWN_COMMON_RECEIVER_MEMBER_OUTSIDE_EFFECT_CLOSURE",
            "UNKNOWN_REQUIRED_BINDING_COORDINATOR_AUTHORITY_CONTEXT_CUSTODY",
        ),
        "unclosed_callable_carriers": tuple(escapes),
    }
