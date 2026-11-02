"""Finite actual provider owner/mint/caller transfer source contract."""

import ast
import copy

from tests.helpers.web_reserve_proposal_child_transfers import binding_nodes, import_origin, owned_origin, scope_nodes

CONTRACTS = {
    "elspeth.web.composer.provider_quota:_bind_attempt_call": 'def _bind_attempt_call(call: ComposerLLMCall, attempt: ProviderAttempt, admitted_ns: int) -> ComposerLLMCall:\n    """Anchor monotonic call duration to the authoritative database admission.\n\n    The request\'s local wall clock is useful only before quota binding. Both\n    persisted endpoints must share the database clock domain, including when\n    a replica\'s wall clock is ahead of or behind UTC midnight.\n    """\n    if call.call_id == attempt.attempt_id and call.started_at == attempt.started_at:\n        return call\n    elapsed_ns = time.monotonic_ns() - admitted_ns\n    return replace(call, call_id=attempt.attempt_id, started_at=attempt.started_at, finished_at=attempt.started_at + timedelta(microseconds=elapsed_ns // 1000))',
    "elspeth.web.composer.provider_quota:ProviderInvocationFamily": "class ProviderInvocationFamily(IntEnum):\n    PRIMARY = 0\n    ADVISOR = 1\n    TITLE = 3",
    "elspeth.web.composer.provider_quota:ProviderInvocationOwner": "class ProviderInvocationOwner:\n    \"\"\"Single explicit mint owner for independently ordered provider families.\"\"\"\n\n    def __init__(self, *, service: SessionServiceProtocol, required_work: RequiredWorkBinding) -> None:\n        if type(required_work) is not RequiredWorkBinding or required_work.role is not RequiredWorkRole.TURN:\n            raise AuditIntegrityError('Provider invocation owner requires its exact turn binding')\n        required_work.validate_context(required_work.coordinator.authority.context)\n        self._service = service\n        self._required_work = required_work\n        self._counters = dict.fromkeys(ProviderInvocationFamily, 0)\n        self._cost_budget: CommonProviderCostBudget | None = None\n\n    @property\n    def service(self) -> SessionServiceProtocol:\n        return self._service\n\n    @property\n    def required_work(self) -> RequiredWorkBinding:\n        return self._required_work\n\n    @property\n    def cost_budget(self) -> CommonProviderCostBudget | None:\n        return self._cost_budget\n\n    def bind_cost_budget(self, budget: CommonProviderCostBudget) -> None:\n        if type(budget) is not CommonProviderCostBudget:\n            raise AuditIntegrityError('provider owner requires nominal request policy')\n        if self._cost_budget is not None:\n            if self._cost_budget is not budget:\n                raise AuditIntegrityError('provider owner cannot replace request policy')\n            return\n        if self._counters[ProviderInvocationFamily.PRIMARY] or self._counters[ProviderInvocationFamily.ADVISOR]:\n            raise AuditIntegrityError('provider policy cannot start after covered invocation')\n        self._cost_budget = budget\n\n    def mint(self, family: ProviderInvocationFamily) -> ProviderCallCustody:\n        if type(family) is not ProviderInvocationFamily:\n            raise AuditIntegrityError('Provider family is not an owned nominal identity')\n        ordinal = self._counters[family]\n        self._counters[family] = ordinal + 1\n        diagonal = int(family) + ordinal\n        semantic = diagonal * (diagonal + 1) // 2 + ordinal\n        binding = RequiredWorkBinding(self.required_work.coordinator, self.required_work.transition_ordinal, semantic, RequiredWorkRole.TITLE if family is ProviderInvocationFamily.TITLE else RequiredWorkRole.TURN)\n        return ProviderCallCustody(service=self.service, context=binding.coordinator.authority.context, required_work=binding, cost_budget=self.cost_budget if family in (ProviderInvocationFamily.PRIMARY, ProviderInvocationFamily.ADVISOR) else None, family=family)",
    "elspeth.web.composer.provider_quota:ProviderCallCustody": "class ProviderCallCustody:\n    \"\"\"Explicit owned admission, physical dispatch and audit settlement custody.\"\"\"\n    __slots__ = ('__weakref__', '_admitted_ns', '_admitting', '_context', '_cost_budget', '_cost_response_accountable', '_family', '_primary_no_sdk_issuer', '_required_work', '_sdk_entered', '_semantic_no_sdk_issuer', '_service', '_settlements', '_undispatched_recurrence', 'attempt', 'call', 'continuation_primary_clock', 'continuation_title', 'continuation_title_result', 'continuation_title_selected')\n\n    def __init__(self, *, service: SessionServiceProtocol, context: SessionOperationContext, required_work: RequiredWorkBinding, cost_budget: CommonProviderCostBudget | None=None, family: ProviderInvocationFamily | None=None) -> None:\n        if type(required_work) is not RequiredWorkBinding:\n            raise AuditIntegrityError('Provider custody requires a nominal explicit binding')\n        required_work.validate_context(context)\n        if context.operation_kind is not SessionOperationKind.COMPOSE:\n            raise AuditIntegrityError('Provider custody requires exact COMPOSE authority')\n        if cost_budget is not None and type(cost_budget) is not CommonProviderCostBudget:\n            raise AuditIntegrityError('provider custody received foreign cost policy')\n        if family is not None and type(family) is not ProviderInvocationFamily:\n            raise AuditIntegrityError('Provider custody family requires its owned nominal discriminator')\n        self._family = family\n        self._cost_budget = cost_budget\n        self._cost_response_accountable = False\n        self._service = service\n        self._context = context\n        self._required_work = required_work\n        self._primary_no_sdk_issuer: PrimaryNoSdkIssuer | None = None\n        self._semantic_no_sdk_issuer: SemanticAdvisorNoSdkIssuer | None = None\n        self.attempt: ProviderAttempt | None = None\n        self.call: ComposerLLMCall | None = None\n        self._sdk_entered = False\n        self._admitting = False\n        self._admitted_ns = 0\n        self._undispatched_recurrence = 0\n        self._settlements = 0\n        self.continuation_title: PreparedTitleProvider | None = None\n        self.continuation_primary_clock: ContinuationActionClock | None = None\n        self.continuation_title_selected: StoredContinuationSnapshot | None = None\n        self.continuation_title_result: TitleAuditSettlementRequest | None = None\n\n    def bind_continuation_title(self, prepared: PreparedTitleProvider) -> None:\n        from elspeth.web.composer.continuation_clock import ContinuationActionClock\n        from elspeth.web.sessions.continuation_title_provider import PreparedTitleProvider\n        if type(prepared) is not PreparedTitleProvider or self.continuation_title is not None or self.attempt is not None or (self.required_work.role is not RequiredWorkRole.TITLE) or (self.required_work.semantic_ordinal != 6) or (self._cost_budget is not None) or (type(prepared.clock) is not ContinuationActionClock) or (prepared.work.coordinator is not self.required_work.coordinator) or (prepared.work.transition_ordinal != self.required_work.transition_ordinal) or (prepared.running.session_operation_context is not self.context) or (prepared.composer._require_sessions_service() is not self.service):\n            raise AuditIntegrityError('Durable TITLE requires its actual first family-3 invocation and original context')\n        self.continuation_title = prepared\n        self.continuation_title_selected = prepared.selected\n\n    def bind_continuation_primary_clock(self, clock: ContinuationActionClock) -> None:\n        from elspeth.web.composer.continuation_clock import ContinuationActionClock\n        if type(clock) is not ContinuationActionClock or self.continuation_title is not None or self.continuation_primary_clock is not None or (self.attempt is not None) or (self.required_work.role is not RequiredWorkRole.TURN):\n            raise AuditIntegrityError('PRIMARY timing requires its original pre-dispatch action owner')\n        self.continuation_primary_clock = clock\n\n    def bind_primary_no_sdk_issuer(self, issuer: PrimaryNoSdkIssuer) -> None:\n        from elspeth.web.composer.continuation_primary_no_sdk import PrimaryNoSdkIssuer\n        if type(issuer) is not PrimaryNoSdkIssuer or self._primary_no_sdk_issuer is not None or self.continuation_primary_clock is not issuer.clock or (self.attempt is not None):\n            raise AuditIntegrityError('PRIMARY no-SDK issuer requires original unentered physical custody')\n        issuer.bind_custody(self)\n        self._primary_no_sdk_issuer = issuer\n\n    @property\n    def family(self) -> ProviderInvocationFamily | None:\n        return self._family\n\n    @property\n    def continuation_semantic_clock(self) -> ContinuationActionClock | None:\n        issuer = self._semantic_no_sdk_issuer\n        if issuer is None:\n            return None\n        if self.family is not ProviderInvocationFamily.ADVISOR:\n            raise AuditIntegrityError('Semantic timing requires its nominal ADVISOR mint family')\n        issuer.require_original_clock()\n        return issuer.clock\n\n    def bind_semantic_no_sdk_issuer(self, issuer: SemanticAdvisorNoSdkIssuer) -> None:\n        from elspeth.web.composer.continuation_semantic_no_sdk import SemanticAdvisorNoSdkIssuer\n        if type(issuer) is not SemanticAdvisorNoSdkIssuer or self.family is not ProviderInvocationFamily.ADVISOR or self._semantic_no_sdk_issuer is not None or (self._primary_no_sdk_issuer is not None) or (self.continuation_primary_clock is not None) or (self.continuation_title is not None) or (self.continuation_semantic_clock is not None) or (self.attempt is not None):\n            raise AuditIntegrityError('Semantic timing and no-SDK authority require original ADVISOR custody')\n        issuer.bind_custody(self)\n        self._semantic_no_sdk_issuer = issuer\n\n    async def retain_continuation_title(self, completion: _AdmittedLLMCompletion) -> None:\n        from elspeth.web.composer.continuation_title_provider import retain_current_title_success\n        if self.continuation_title is None or self.call is None or (not self._sdk_entered):\n            raise AuditIntegrityError('TITLE retention requires its actual physical success and original audit')\n        _, cancellations = await self._join(retain_current_title_success(self, completion))\n        self.raise_deferred_cancellations(cancellations)\n\n    def prepare_cost_request(self, kwargs: CommonProviderCostRequest) -> None:\n        if self._cost_budget is not None:\n            self._cost_budget.prepare_request(kwargs)\n\n    def cost_completion_limit(self) -> int | None:\n        if self._cost_budget is None:\n            return None\n        policy = self._cost_budget.snapshot().policy\n        return policy.max_completion_tokens if policy is not None else None\n\n    @property\n    def has_cost_budget(self) -> bool:\n        return self._cost_budget is not None\n\n    def require_cost_dispatch(self) -> None:\n        if self._cost_budget is not None:\n            self._cost_budget.require_dispatch()\n\n    def require_cost_output(self) -> None:\n        if self._cost_budget is not None:\n            self._cost_budget.require_output()\n\n    def mark_cost_response_accountable(self, *, admitted_metadata: bool) -> None:\n        if type(admitted_metadata) is not bool or self.attempt is None or (not self._sdk_entered):\n            raise AuditIntegrityError('response accountability requires actual dispatched custody')\n        self._cost_response_accountable = admitted_metadata\n\n    @property\n    def service(self) -> SessionServiceProtocol:\n        return self._service\n\n    @property\n    def context(self) -> SessionOperationContext:\n        return self._context\n\n    @property\n    def required_work(self) -> RequiredWorkBinding:\n        return self._required_work\n\n    async def _join[R](self, operation: Coroutine[Any, Any, R]) -> tuple[R, tuple[asyncio.CancelledError, ...]]:\n        task = asyncio.create_task(operation)\n        cancellations: list[asyncio.CancelledError] = []\n        while not task.done():\n            try:\n                await asyncio.shield(task)\n            except asyncio.CancelledError as cancellation:\n                if all((cancellation is not original for original in cancellations)):\n                    cancellations.append(cancellation)\n            except BaseException:\n                break\n        if task.cancelled():\n            try:\n                task.result()\n            except asyncio.CancelledError as child_cancellation:\n                integrity = AuditIntegrityError('Required provider child lacks an actual delivered outcome')\n                integrity.__cause__ = child_cancellation\n                if cancellations:\n                    raise BaseExceptionGroup('Provider self-cancelled child and original cancellations', [integrity, child_cancellation, *cancellations]) from None\n                raise integrity from child_cancellation\n        try:\n            return (task.result(), tuple(cancellations))\n        except BaseException as failure:\n            retained = failure\n            if isinstance(failure, Exception) and (not isinstance(failure, (*contract_errors.TIER_1_ERRORS, SQLAlchemyError, SessionOperationFenceLost, ChargeableAdmissionRefused, BaseExceptionGroup))):\n                retained = ComposerOwnedSettlementFailure()\n                retained.__cause__ = failure\n            if cancellations:\n                raise BaseExceptionGroup('Provider required child and cancellation outcomes', [retained, *cancellations]) from None\n            if retained is not failure:\n                raise retained from failure\n            raise\n\n    def raise_deferred_cancellations(self, cancellations: tuple[asyncio.CancelledError, ...]) -> None:\n        if len(cancellations) == 1:\n            raise cancellations[0]\n        if cancellations:\n            raise BaseExceptionGroup('Provider original cancellations', list(cancellations))\n\n    async def join_title_update[R](self, operation: Coroutine[Any, Any, R]) -> tuple[R, tuple[asyncio.CancelledError, ...]]:\n        if self.required_work.role is not RequiredWorkRole.TITLE or self.attempt is not None:\n            raise AuditIntegrityError('Title publication cannot precede provider accounting completion')\n        return await self._join(operation)\n\n    async def _cancel_undispatched(self, *, model: str) -> tuple[asyncio.CancelledError, ...]:\n        if self.attempt is None or self._sdk_entered:\n            raise AuditIntegrityError('Undispatched disposition lacks exact no-SDK attempt evidence')\n        admitted = self.attempt\n        binding = self.required_work\n        ticket = binding.coordinator.reserve(RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL, transition_ordinal=binding.transition_ordinal, semantic_ordinal=binding.semantic_ordinal, recurrence_ordinal=self._undispatched_recurrence)\n        self._undispatched_recurrence += 1\n        _, cancellations = await self._join(self.service.cancel_undispatched_provider_attempt(session_operation_context=self.context, attempt_id=self.attempt.attempt_id, requested_model=model, required_work=ticket))\n        if self._primary_no_sdk_issuer is not None:\n            self._primary_no_sdk_issuer.note_undispatched_cancel(self, admitted, model=model)\n        if self._semantic_no_sdk_issuer is not None:\n            self._semantic_no_sdk_issuer.note_undispatched_cancel(self, admitted, model=model)\n        self.attempt = None\n        self.call = None\n        return cancellations\n\n    async def cancel_before_sdk(self, *, model: str) -> tuple[asyncio.CancelledError, ...]:\n        \"\"\"Dispose an actually admitted attempt whose physical SDK never began.\"\"\"\n        return await self._cancel_undispatched(model=model)\n\n    async def admit(self, *, model: str) -> None:\n        if type(model) is not str or not model or self._admitting:\n            raise AuditIntegrityError('Provider admission lacks a single owned model invocation')\n        self._admitting = True\n        try:\n            await self.settle()\n            self.require_cost_dispatch()\n            self._cost_response_accountable = False\n            title = self.required_work.role is RequiredWorkRole.TITLE\n            sql, projection = self.required_work.reserve_pair(RequiredWorkSource.TITLE_PROVIDER_ADMISSION_SQL if title else RequiredWorkSource.PROVIDER_ADMISSION_SQL, RequiredWorkSource.TITLE_PROVIDER_ADMISSION_PROJECTION if title else RequiredWorkSource.PROVIDER_ADMISSION_PROJECTION)\n            try:\n                if self.continuation_title is None:\n                    attempt, cancellations = await self._join(self.service.begin_provider_attempt(session_operation_context=self.context, source='auto_title' if title else 'composer', required_work=sql))\n                else:\n                    from elspeth.web.sessions.continuation_title_provider import TitleProviderAdmission\n                    from elspeth.web.sessions.service import SessionServiceImpl\n                    if type(self.service) is not SessionServiceImpl:\n                        raise AuditIntegrityError('TITLE admission requires its actual sessions owner')\n                    admitted, cancellations = await self._join(self.service.begin_continuation_title_attempt(prepared=self.continuation_title, requested_model=model, required_work=sql))\n                    if type(admitted) is not TitleProviderAdmission:\n                        raise AuditIntegrityError('TITLE admission lost its exact physical/source result')\n                    attempt = admitted.attempt\n                    self.continuation_title_selected = admitted.selected\n            except BaseException:\n                if sql.complete:\n                    projection.complete_without_submission()\n                raise\n            projection.begin_projection()\n            try:\n                if type(attempt) is not ProviderAttempt:\n                    raise AuditIntegrityError('Provider admission returned a foreign attempt')\n                self.attempt = attempt\n                if self._primary_no_sdk_issuer is not None:\n                    self._primary_no_sdk_issuer.note_admitted_attempt(self, attempt, model=model)\n                if self._semantic_no_sdk_issuer is not None:\n                    self._semantic_no_sdk_issuer.note_admitted_attempt(self, attempt, model=model)\n                self._admitted_ns = time.monotonic_ns()\n                self.call = None\n                self._sdk_entered = False\n            except BaseException as failure:\n                projection.complete_owned(failure)\n                raise\n            projection.complete_owned()\n            if cancellations:\n                additional = await self._cancel_undispatched(model=model)\n                self.raise_deferred_cancellations((*cancellations, *additional))\n        finally:\n            self._admitting = False\n\n    def mark_sdk_entered(self) -> None:\n        if self.attempt is None or self._sdk_entered or self.call is not None:\n            raise AuditIntegrityError('Physical provider dispatch lacks exact admitted custody')\n        if self._primary_no_sdk_issuer is not None:\n            self._primary_no_sdk_issuer.mark_sdk_entered(self)\n        if self._semantic_no_sdk_issuer is not None:\n            self._semantic_no_sdk_issuer.mark_sdk_entered(self)\n        if self._cost_budget is not None:\n            self._cost_budget.dispatched()\n        self._sdk_entered = True\n\n    def has_undispatched_attempt(self) -> bool:\n        return self.attempt is not None and (not self._sdk_entered)\n\n    def needs_terminal_audit(self) -> bool:\n        return self.attempt is not None and self._sdk_entered and (self.call is None)\n\n    def bind_call(self, call: ComposerLLMCall) -> ComposerLLMCall:\n        if type(call) is not ComposerLLMCall:\n            raise AuditIntegrityError('Provider audit is not an owned complete call')\n        if self.attempt is None or not self._sdk_entered:\n            raise AuditIntegrityError('Provider audit lacks actual dispatched admission')\n        bound = _bind_attempt_call(call, self.attempt, self._admitted_ns)\n        self.call = bound\n        return bound\n\n    def retain_audit(self, call: ComposerLLMCall) -> bool:\n        if type(call) is not ComposerLLMCall:\n            raise AuditIntegrityError('Provider retained audit is not nominal')\n        if self.attempt is None:\n            return False\n        if not self._sdk_entered or call.call_id != self.attempt.attempt_id or call.started_at != self.attempt.started_at:\n            raise AuditIntegrityError('Provider audit replaced actual admitted identity')\n        if self._cost_budget is not None and self._cost_budget.snapshot().applicability == 'continuation_v2' and (call.cost_response_accountable is None or call.cost_response_accountable is not self._cost_response_accountable):\n            raise AuditIntegrityError('Continuation audit changed its actual response accountability')\n        self.call = call\n        return True\n\n    async def settle(self) -> None:\n        if self.attempt is None:\n            return\n        if not self._sdk_entered or self.call is None:\n            raise AuditIntegrityError('Required provider attempt lacks complete terminal audit')\n        current_title: TitleAuditSettlementRequest | None = None\n        if self.continuation_title_result is not None:\n            from elspeth.web.composer.continuation_title_provider import refresh_current_title_settlement\n            from elspeth.web.sessions.service import SessionServiceImpl\n            if type(self.service) is not SessionServiceImpl:\n                raise AuditIntegrityError('TITLE settlement requires its actual sessions writer')\n            current_title = await refresh_current_title_settlement(self)\n        binding = self.required_work\n        projection: RequiredWorkTicket | None = None\n        if binding.role is RequiredWorkRole.TITLE:\n            sql = binding.coordinator.reserve(RequiredWorkSource.TITLE_PROVIDER_SETTLEMENT_SQL, transition_ordinal=binding.transition_ordinal, semantic_ordinal=binding.semantic_ordinal, recurrence_ordinal=self._settlements)\n            self._settlements += 1\n        else:\n            sql, projection = binding.reserve_pair(RequiredWorkSource.PROVIDER_SETTLEMENT_SQL, RequiredWorkSource.PROVIDER_SETTLEMENT_PROJECTION)\n        try:\n            if self.continuation_title_result is None:\n                _, cancellations = await self._join(self.service.finish_provider_attempt(session_operation_context=self.context, call=self.call, required_work=sql))\n            else:\n                from elspeth.web.sessions.service import SessionServiceImpl\n                if type(self.service) is not SessionServiceImpl or current_title is None:\n                    raise AuditIntegrityError('TITLE settlement lost its actual current source result')\n                _, cancellations = await self._join(self.service.finish_provider_attempt(session_operation_context=self.context, call=current_title.audit_call, required_work=sql, continuation_title=current_title))\n        except BaseException:\n            if projection is not None and sql.complete:\n                projection.complete_without_submission()\n            raise\n        if projection is not None:\n            projection.begin_projection()\n            projection.complete_owned()\n        if self._cost_budget is not None:\n            if self._cost_budget.snapshot().applicability == 'continuation_v2' and (self.call.cost_response_accountable is None or self.call.cost_response_accountable is not self._cost_response_accountable):\n                raise AuditIntegrityError('Continuation settlement lost its observed response accountability')\n            self._cost_budget.consume_settled(self.call, response_accountable=self._cost_response_accountable)\n        self.attempt = None\n        self.call = None\n        self._sdk_entered = False\n        self.raise_deferred_cancellations(cancellations)",
    "elspeth.web.composer.continuation_title_provider:run_prepared_title_provider": "async def run_prepared_title_provider(*, prepared: PreparedTitleProvider, invocation_owner: ProviderInvocationOwner) -> TitleAuditSettlementRequest | None:\n    if type(prepared) is not PreparedTitleProvider or type(invocation_owner) is not ProviderInvocationOwner:\n        raise AuditIntegrityError('TITLE execution requires its actual prepared/current invocation owners')\n    if invocation_owner.required_work is not prepared.work or invocation_owner.service is not _sessions(prepared.composer):\n        raise AuditIntegrityError('TITLE execution changed its actual original work/service')\n    root = decode_continuation_snapshot(prepared.selected.payload_json)\n    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)\n    if type(policy) is ContinuationAdmissionPolicyV3:\n\n        def refuse_unbounded() -> TitleNoSdkRefusal:\n            with locked_session_transaction(prepared.authority._engine, str(prepared.running.claim.session_id)) as conn:\n                cold = read_v3_title_no_sdk_refusal_on_connection(conn, session_id=prepared.running.claim.session_id, operation_id=prepared.running.claim.operation_id, expected_prepared=prepared)\n                if cold is not None:\n                    return cold\n                return refuse_v3_title_unbounded_on_connection(conn, prepared)\n        await _registered(work=prepared.work, source=RequiredWorkSource.CONTINUATION_STATE_SQL, operation=refuse_unbounded, expected=TitleNoSdkRefusal)\n        return None\n    custody = invocation_owner.mint(ProviderInvocationFamily.TITLE)\n    custody.bind_continuation_title(prepared)\n    settings = prepared.composer._settings\n    request = prepared.request\n    from elspeth.web.composer.continuation_bootstrap import bootstrap_values, decode_continuation_bootstrap\n    from elspeth.web.sessions.continuation_title_provider import _chunk\n\n    def original_user() -> str:\n        with locked_session_transaction(prepared.authority._engine, str(prepared.running.claim.session_id)) as conn:\n            _current(conn, prepared.running, prepared.selected)\n            frame = decode_continuation_snapshot(prepared.selected.payload_json).frames[0]\n            if type(frame) is not InitializeFrame or frame.bootstrap is None:\n                raise AuditIntegrityError('TITLE producer lost its original bootstrap')\n            values = bootstrap_values(decode_continuation_bootstrap(_chunk(conn, prepared.running, frame.bootstrap)))\n            original_request = values.request\n            if type(original_request) is not SendMessageRequest:\n                raise AuditIntegrityError('TITLE producer lost its actual original SendMessage request')\n            return original_request.content\n    user = await _registered(work=prepared.work, source=RequiredWorkSource.CONTINUATION_READ_SQL, operation=original_user, expected=str)\n    await _maybe_auto_title_session_required(service=_sessions(prepared.composer), session_id=prepared.running.claim.session_id, model=request.kwargs['model'], temperature=request.kwargs.get('temperature'), seed=request.kwargs.get('seed'), session_operation_context=prepared.running.session_operation_context, kwargs=request.kwargs, audit_messages=request.audit_messages, provider_custody=custody, prompt_guard_mount=prepared.composer.composer_prompt_guard_mount, prompt_guard_mode=settings.composer_prompt_guard_mode, user_message=user, max_prepared_request_bytes=settings.composer_max_prepared_request_bytes, prepared_input=prepared.provider_input)\n    return custody.continuation_title_result",
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
    self._action_ordinal = 0
    self.progress_owner: ContinuationProgressOwner | None = None
    self._progress_cleanup_failures: list[BaseException] = []
    self._iteration_stage: IterationStageData | None = None
    self._action_clock: ContinuationActionClock | None = None
""",
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
    async def dispose(self, original: BaseException | None = None) -> ContinuationSegmentOutcome: pass
    async def advance(self, stored: StoredContinuationSnapshot, selected: SelectedContinuationState) -> MessageWithStateResponse | None: pass
    async def pending_control(self, selected: SelectedContinuationState) -> StoredContinuationControlIntent | None: pass
    async def iteration_stage(self, stored: StoredContinuationSnapshot, selected: SelectedContinuationState) -> IterationStageData | None: pass
    async def yield_control(self, stored: StoredContinuationSnapshot, selected: SelectedContinuationState) -> ContinuationSegmentOutcome: pass
    async def watch_stage(self, stage: asyncio.Task[MessageWithStateResponse | None], reader: ComposerOperationWatchReader) -> None: pass
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
) -> PrimaryProviderResult | PrimaryNoSdkRefusalReceipt | V3UnboundedTailRefusal:
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
policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
if type(policy) is ContinuationAdmissionPolicyV3:
    def refuse_unbounded() -> TitleNoSdkRefusal:
        with locked_session_transaction(prepared.authority._engine, str(prepared.running.claim.session_id)) as conn:
            cold = read_v3_title_no_sdk_refusal_on_connection(conn, session_id=prepared.running.claim.session_id,
                operation_id=prepared.running.claim.operation_id, expected_prepared=prepared)
            if cold is not None:
                return cold
            return refuse_v3_title_unbounded_on_connection(conn, prepared)
    await _registered(work=prepared.work, source=RequiredWorkSource.CONTINUATION_STATE_SQL,
        operation=refuse_unbounded, expected=TitleNoSdkRefusal)
    return None
custody = invocation_owner.mint(ProviderInvocationFamily.TITLE)
custody.bind_continuation_title(prepared)
"""
    ).body
    if not isinstance(title_entry, ast.AsyncFunctionDef) or _shape(title_entry.body[: len(title_prefix)]) != _shape(title_prefix):
        return ["provider current TITLE V3 refusal must precede exact mint"]
    for binding, module in (
        ("ContinuationAdmissionPolicyV3", "elspeth.web.composer.continuation_policy"),
        ("decode_continuation_policy", "elspeth.web.composer.continuation_policy"),
        ("decode_continuation_snapshot", "elspeth.web.composer.continuation_codec"),
        ("_registered", "elspeth.web.composer.continuation_initialization"),
        ("_sessions", "elspeth.web.composer.continuation_initialization"),
        ("locked_session_transaction", "elspeth.web.sessions.locking"),
        ("read_v3_title_no_sdk_refusal_on_connection", "elspeth.web.sessions.continuation_title_provider"),
        ("refuse_v3_title_unbounded_on_connection", "elspeth.web.sessions.continuation_title_provider"),
        ("TitleNoSdkRefusal", "elspeth.web.sessions.continuation_title_provider"),
        ("PreparedTitleProvider", "elspeth.web.sessions.continuation_title_provider"),
        ("RequiredWorkSource", "elspeth.web.required_work"),
    ):
        if import_origin(title.tree, binding, module, binding) is None:
            return ["provider current TITLE refusal supplier changed " + binding]
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
            "CommonProviderCostBudget": "elspeth.web.composer.provider_cost.CommonProviderCostBudget",
            "RequiredWorkBinding": BINDING,
            "RequiredWorkRole": "elspeth.web.required_work.RequiredWorkRole",
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
