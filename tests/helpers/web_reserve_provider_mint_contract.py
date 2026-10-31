"""Finite actual provider owner/mint/caller transfer source contract."""

import ast
import copy

from tests.helpers.web_reserve_proposal_child_transfers import binding_nodes, import_origin, owned_origin, scope_nodes

CONTRACTS = {
    "elspeth.web.composer.provider_quota:_bind_attempt_call": 'def _bind_attempt_call(call: ComposerLLMCall, attempt: ProviderAttempt, admitted_ns: int) -> ComposerLLMCall:\n    """Anchor monotonic call duration to the authoritative database admission.\n\n    The request\'s local wall clock is useful only before quota binding. Both\n    persisted endpoints must share the database clock domain, including when\n    a replica\'s wall clock is ahead of or behind UTC midnight.\n    """\n    if call.call_id == attempt.attempt_id and call.started_at == attempt.started_at:\n        return call\n    elapsed_ns = time.monotonic_ns() - admitted_ns\n    return replace(call, call_id=attempt.attempt_id, started_at=attempt.started_at, finished_at=attempt.started_at + timedelta(microseconds=elapsed_ns // 1000))',
    "elspeth.web.composer.provider_quota:ProviderInvocationFamily": "class ProviderInvocationFamily(IntEnum):\n    PRIMARY = 0\n    ADVISOR = 1\n    PLANNER = 2\n    TITLE = 3",
    "elspeth.web.composer.provider_quota:ProviderInvocationOwner": "class ProviderInvocationOwner:\n    \"\"\"Single explicit mint owner for independently ordered provider families.\"\"\"\n\n    def __init__(self, *, service: SessionServiceProtocol, required_work: RequiredWorkBinding) -> None:\n        if type(required_work) is not RequiredWorkBinding or required_work.role is not RequiredWorkRole.TURN:\n            raise AuditIntegrityError('Provider invocation owner requires its exact turn binding')\n        required_work.validate_context(required_work.coordinator.authority.context)\n        self._service = service\n        self._required_work = required_work\n        self._counters = dict.fromkeys(ProviderInvocationFamily, 0)\n        self._cost_budget: CommonProviderCostBudget | None = None\n\n    @property\n    def service(self) -> SessionServiceProtocol:\n        return self._service\n\n    @property\n    def required_work(self) -> RequiredWorkBinding:\n        return self._required_work\n\n    @property\n    def cost_budget(self) -> CommonProviderCostBudget | None:\n        return self._cost_budget\n\n    def bind_cost_budget(self, budget: CommonProviderCostBudget) -> None:\n        if type(budget) is not CommonProviderCostBudget:\n            raise AuditIntegrityError('provider owner requires nominal request policy')\n        if self._cost_budget is not None:\n            if self._cost_budget is not budget:\n                raise AuditIntegrityError('provider owner cannot replace request policy')\n            return\n        if self._counters[ProviderInvocationFamily.PRIMARY] or self._counters[ProviderInvocationFamily.ADVISOR]:\n            raise AuditIntegrityError('provider policy cannot start after covered invocation')\n        self._cost_budget = budget\n\n    def mint(self, family: ProviderInvocationFamily) -> ProviderCallCustody:\n        if type(family) is not ProviderInvocationFamily:\n            raise AuditIntegrityError('Provider family is not an owned nominal identity')\n        ordinal = self._counters[family]\n        self._counters[family] = ordinal + 1\n        diagonal = int(family) + ordinal\n        semantic = diagonal * (diagonal + 1) // 2 + ordinal\n        binding = RequiredWorkBinding(self.required_work.coordinator, self.required_work.transition_ordinal, semantic, RequiredWorkRole.TITLE if family is ProviderInvocationFamily.TITLE else RequiredWorkRole.TURN)\n        return ProviderCallCustody(service=self.service, context=binding.coordinator.authority.context, required_work=binding, cost_budget=self.cost_budget if family in (ProviderInvocationFamily.PRIMARY, ProviderInvocationFamily.ADVISOR) else None)",
    "elspeth.web.composer.provider_quota:ProviderCallCustody": 'class ProviderCallCustody:\n    """Explicit owned admission, physical dispatch and audit settlement custody."""\n\n    __slots__ = (\n        "__weakref__",\n        "_admitted_ns",\n        "_admitting",\n        "_context",\n        "_cost_budget",\n        "_cost_response_accountable",\n        "_required_work",\n        "_sdk_entered",\n        "_service",\n        "_settlements",\n        "_undispatched_recurrence",\n        "attempt",\n        "call",\n        "continuation_title",\n        "continuation_title_result",\n        "continuation_title_selected",\n    )\n\n    def __init__(\n        self,\n        *,\n        service: SessionServiceProtocol,\n        context: SessionOperationContext,\n        required_work: RequiredWorkBinding,\n        cost_budget: CommonProviderCostBudget | None = None,\n    ) -> None:\n        if type(required_work) is not RequiredWorkBinding:\n            raise AuditIntegrityError("Provider custody requires a nominal explicit binding")\n        required_work.validate_context(context)\n        if context.operation_kind is not SessionOperationKind.COMPOSE:\n            raise AuditIntegrityError("Provider custody requires exact COMPOSE authority")\n        if cost_budget is not None and type(cost_budget) is not CommonProviderCostBudget:\n            raise AuditIntegrityError("provider custody received foreign cost policy")\n        self._cost_budget = cost_budget\n        self._cost_response_accountable = False\n        self._service = service\n        self._context = context\n        self._required_work = required_work\n        self.attempt: ProviderAttempt | None = None\n        self.call: ComposerLLMCall | None = None\n        self._sdk_entered = False\n        self._admitting = False\n        self._admitted_ns = 0\n        self._undispatched_recurrence = 0\n        self._settlements = 0\n        self.continuation_title: PreparedTitleProvider | None = None\n        self.continuation_title_selected: StoredContinuationSnapshot | None = None\n        self.continuation_title_result: TitleAuditSettlementRequest | None = None\n\n    def bind_continuation_title(self, prepared: PreparedTitleProvider) -> None:\n        from elspeth.web.composer.continuation_clock import ContinuationActionClock\n        from elspeth.web.sessions.continuation_title_provider import PreparedTitleProvider\n\n        if (\n            type(prepared) is not PreparedTitleProvider\n            or self.continuation_title is not None\n            or self.attempt is not None\n            or self.required_work.role is not RequiredWorkRole.TITLE\n            or self.required_work.semantic_ordinal != 6\n            or self._cost_budget is not None\n            or type(prepared.clock) is not ContinuationActionClock\n            or prepared.work.coordinator is not self.required_work.coordinator\n            or prepared.work.transition_ordinal != self.required_work.transition_ordinal\n            or prepared.running.session_operation_context is not self.context\n            or prepared.composer._require_sessions_service() is not self.service\n        ):\n            raise AuditIntegrityError("Durable TITLE requires its actual first family-3 invocation and original context")\n        self.continuation_title = prepared\n        self.continuation_title_selected = prepared.selected\n\n    async def retain_continuation_title(self, completion: _AdmittedLLMCompletion) -> None:\n        from elspeth.web.composer.continuation_title_provider import retain_current_title_success\n\n        if self.continuation_title is None or self.call is None or not self._sdk_entered:\n            raise AuditIntegrityError("TITLE retention requires its actual physical success and original audit")\n        _, cancellations = await self._join(retain_current_title_success(self, completion))\n        self.raise_deferred_cancellations(cancellations)\n\n    def prepare_cost_request(self, kwargs: CommonProviderCostRequest) -> None:\n        if self._cost_budget is not None:\n            self._cost_budget.prepare_request(kwargs)\n\n    def cost_completion_limit(self) -> int | None:\n        if self._cost_budget is None:\n            return None\n        policy = self._cost_budget.snapshot().policy\n        return policy.max_completion_tokens if policy is not None else None\n\n    def require_cost_dispatch(self) -> None:\n        if self._cost_budget is not None:\n            self._cost_budget.require_dispatch()\n\n    def require_cost_output(self) -> None:\n        if self._cost_budget is not None:\n            self._cost_budget.require_output()\n\n    def mark_cost_response_accountable(self, *, admitted_metadata: bool) -> None:\n        if type(admitted_metadata) is not bool or self.attempt is None or not self._sdk_entered:\n            raise AuditIntegrityError("response accountability requires actual dispatched custody")\n        self._cost_response_accountable = admitted_metadata\n\n    @property\n    def service(self) -> SessionServiceProtocol:\n        return self._service\n\n    @property\n    def context(self) -> SessionOperationContext:\n        return self._context\n\n    @property\n    def required_work(self) -> RequiredWorkBinding:\n        return self._required_work\n\n    async def _join[R](self, operation: Coroutine[Any, Any, R]) -> tuple[R, tuple[asyncio.CancelledError, ...]]:\n        task = asyncio.create_task(operation)\n        cancellations: list[asyncio.CancelledError] = []\n        while not task.done():\n            try:\n                await asyncio.shield(task)\n            except asyncio.CancelledError as cancellation:\n                if all(cancellation is not original for original in cancellations):\n                    cancellations.append(cancellation)\n            except BaseException:\n                break\n        if task.cancelled():\n            try:\n                task.result()\n            except asyncio.CancelledError as child_cancellation:\n                integrity = AuditIntegrityError("Required provider child lacks an actual delivered outcome")\n                integrity.__cause__ = child_cancellation\n                if cancellations:\n                    raise BaseExceptionGroup(\n                        "Provider self-cancelled child and original cancellations", [integrity, child_cancellation, *cancellations]\n                    ) from None\n                raise integrity from child_cancellation\n        try:\n            return task.result(), tuple(cancellations)\n        except BaseException as failure:\n            retained = failure\n            if isinstance(failure, Exception) and not isinstance(\n                failure,\n                (\n                    *contract_errors.TIER_1_ERRORS,\n                    SQLAlchemyError,\n                    SessionOperationFenceLost,\n                    ChargeableAdmissionRefused,\n                    BaseExceptionGroup,\n                ),\n            ):\n                retained = ComposerOwnedSettlementFailure()\n                retained.__cause__ = failure\n            if cancellations:\n                raise BaseExceptionGroup("Provider required child and cancellation outcomes", [retained, *cancellations]) from None\n            if retained is not failure:\n                raise retained from failure\n            raise\n\n    def raise_deferred_cancellations(self, cancellations: tuple[asyncio.CancelledError, ...]) -> None:\n        if len(cancellations) == 1:\n            raise cancellations[0]\n        if cancellations:\n            raise BaseExceptionGroup("Provider original cancellations", list(cancellations))\n\n    async def join_title_update[R](self, operation: Coroutine[Any, Any, R]) -> tuple[R, tuple[asyncio.CancelledError, ...]]:\n        if self.required_work.role is not RequiredWorkRole.TITLE or self.attempt is not None:\n            raise AuditIntegrityError("Title publication cannot precede provider accounting completion")\n        return await self._join(operation)\n\n    async def _cancel_undispatched(self, *, model: str) -> tuple[asyncio.CancelledError, ...]:\n        if self.attempt is None or self._sdk_entered:\n            raise AuditIntegrityError("Undispatched disposition lacks exact no-SDK attempt evidence")\n        binding = self.required_work\n        ticket = binding.coordinator.reserve(\n            RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL,\n            transition_ordinal=binding.transition_ordinal,\n            semantic_ordinal=binding.semantic_ordinal,\n            recurrence_ordinal=self._undispatched_recurrence,\n        )\n        self._undispatched_recurrence += 1\n        _, cancellations = await self._join(\n            self.service.cancel_undispatched_provider_attempt(\n                session_operation_context=self.context,\n                attempt_id=self.attempt.attempt_id,\n                requested_model=model,\n                required_work=ticket,\n            )\n        )\n        self.attempt = None\n        self.call = None\n        return cancellations\n\n    async def cancel_before_sdk(self, *, model: str) -> tuple[asyncio.CancelledError, ...]:\n        """Dispose an actually admitted attempt whose physical SDK never began."""\n        return await self._cancel_undispatched(model=model)\n\n    async def admit(self, *, model: str) -> None:\n        if type(model) is not str or not model or self._admitting:\n            raise AuditIntegrityError("Provider admission lacks a single owned model invocation")\n        self._admitting = True\n        try:\n            await self.settle()\n            self.require_cost_dispatch()\n            self._cost_response_accountable = False\n            title = self.required_work.role is RequiredWorkRole.TITLE\n            sql, projection = self.required_work.reserve_pair(\n                RequiredWorkSource.TITLE_PROVIDER_ADMISSION_SQL if title else RequiredWorkSource.PROVIDER_ADMISSION_SQL,\n                RequiredWorkSource.TITLE_PROVIDER_ADMISSION_PROJECTION if title else RequiredWorkSource.PROVIDER_ADMISSION_PROJECTION,\n            )\n            try:\n                if self.continuation_title is None:\n                    attempt, cancellations = await self._join(\n                        self.service.begin_provider_attempt(\n                            session_operation_context=self.context,\n                            source="auto_title" if title else "composer",\n                            required_work=sql,\n                        )\n                    )\n                else:\n                    from elspeth.web.sessions.continuation_title_provider import TitleProviderAdmission\n                    from elspeth.web.sessions.service import SessionServiceImpl\n\n                    if type(self.service) is not SessionServiceImpl:\n                        raise AuditIntegrityError("TITLE admission requires its actual sessions owner")\n                    admitted, cancellations = await self._join(\n                        self.service.begin_continuation_title_attempt(\n                            prepared=self.continuation_title, requested_model=model, required_work=sql\n                        )\n                    )\n                    if type(admitted) is not TitleProviderAdmission:\n                        raise AuditIntegrityError("TITLE admission lost its exact physical/source result")\n                    attempt = admitted.attempt\n                    self.continuation_title_selected = admitted.selected\n            except BaseException:\n                if sql.complete:\n                    projection.complete_without_submission()\n                raise\n            projection.begin_projection()\n            try:\n                if type(attempt) is not ProviderAttempt:\n                    raise AuditIntegrityError("Provider admission returned a foreign attempt")\n                self.attempt = attempt\n                self._admitted_ns = time.monotonic_ns()\n                self.call = None\n                self._sdk_entered = False\n            except BaseException as failure:\n                projection.complete_owned(failure)\n                raise\n            projection.complete_owned()\n            if cancellations:\n                additional = await self._cancel_undispatched(model=model)\n                self.raise_deferred_cancellations((*cancellations, *additional))\n        finally:\n            self._admitting = False\n\n    def mark_sdk_entered(self) -> None:\n        if self.attempt is None or self._sdk_entered or self.call is not None:\n            raise AuditIntegrityError("Physical provider dispatch lacks exact admitted custody")\n        if self._cost_budget is not None:\n            self._cost_budget.dispatched()\n        self._sdk_entered = True\n\n    def has_undispatched_attempt(self) -> bool:\n        return self.attempt is not None and not self._sdk_entered\n\n    def needs_terminal_audit(self) -> bool:\n        return self.attempt is not None and self._sdk_entered and self.call is None\n\n    def bind_call(self, call: ComposerLLMCall) -> ComposerLLMCall:\n        if type(call) is not ComposerLLMCall:\n            raise AuditIntegrityError("Provider audit is not an owned complete call")\n        if self.attempt is None or not self._sdk_entered:\n            raise AuditIntegrityError("Provider audit lacks actual dispatched admission")\n        bound = _bind_attempt_call(call, self.attempt, self._admitted_ns)\n        self.call = bound\n        return bound\n\n    def retain_audit(self, call: ComposerLLMCall) -> bool:\n        if type(call) is not ComposerLLMCall:\n            raise AuditIntegrityError("Provider retained audit is not nominal")\n        if self.attempt is None:\n            return False\n        if not self._sdk_entered or call.call_id != self.attempt.attempt_id or call.started_at != self.attempt.started_at:\n            raise AuditIntegrityError("Provider audit replaced actual admitted identity")\n        self.call = call\n        return True\n\n    async def settle(self) -> None:\n        if self.attempt is None:\n            return\n        if not self._sdk_entered or self.call is None:\n            raise AuditIntegrityError("Required provider attempt lacks complete terminal audit")\n        current_title: TitleAuditSettlementRequest | None = None\n        if self.continuation_title_result is not None:\n            from elspeth.web.composer.continuation_title_provider import refresh_current_title_settlement\n            from elspeth.web.sessions.service import SessionServiceImpl\n\n            if type(self.service) is not SessionServiceImpl:\n                raise AuditIntegrityError("TITLE settlement requires its actual sessions writer")\n            # Current reads may refuse or be cancelled. Do not allocate the\n            # accounting receipt until it can be handed to the real finisher.\n            current_title = await refresh_current_title_settlement(self)\n        binding = self.required_work\n        projection: RequiredWorkTicket | None = None\n        if binding.role is RequiredWorkRole.TITLE:\n            sql = binding.coordinator.reserve(\n                RequiredWorkSource.TITLE_PROVIDER_SETTLEMENT_SQL,\n                transition_ordinal=binding.transition_ordinal,\n                semantic_ordinal=binding.semantic_ordinal,\n                recurrence_ordinal=self._settlements,\n            )\n            self._settlements += 1\n        else:\n            sql, projection = binding.reserve_pair(\n                RequiredWorkSource.PROVIDER_SETTLEMENT_SQL, RequiredWorkSource.PROVIDER_SETTLEMENT_PROJECTION\n            )\n        try:\n            if self.continuation_title_result is None:\n                _, cancellations = await self._join(\n                    self.service.finish_provider_attempt(\n                        session_operation_context=self.context,\n                        call=self.call,\n                        required_work=sql,\n                    )\n                )\n            else:\n                from elspeth.web.sessions.service import SessionServiceImpl\n\n                if type(self.service) is not SessionServiceImpl or current_title is None:\n                    raise AuditIntegrityError("TITLE settlement lost its actual current source result")\n                _, cancellations = await self._join(\n                    self.service.finish_provider_attempt(\n                        session_operation_context=self.context,\n                        call=current_title.audit_call,\n                        required_work=sql,\n                        continuation_title=current_title,\n                    )\n                )\n        except BaseException:\n            if projection is not None and sql.complete:\n                projection.complete_without_submission()\n            raise\n        if projection is not None:\n            projection.begin_projection()\n            projection.complete_owned()\n        if self._cost_budget is not None:\n            self._cost_budget.consume_settled(self.call, response_accountable=self._cost_response_accountable)\n        self.attempt = None\n        self.call = None\n        self._sdk_entered = False\n        self.raise_deferred_cancellations(cancellations)\n',
    "elspeth.web.composer.continuation_title_provider:run_prepared_title_provider": "async def run_prepared_title_provider(*, prepared: PreparedTitleProvider, invocation_owner: ProviderInvocationOwner) -> TitleAuditSettlementRequest | None:\n    if type(prepared) is not PreparedTitleProvider or type(invocation_owner) is not ProviderInvocationOwner:\n        raise AuditIntegrityError('TITLE execution requires its actual prepared/current invocation owners')\n    if invocation_owner.required_work is not prepared.work or invocation_owner.service is not _sessions(prepared.composer):\n        raise AuditIntegrityError('TITLE execution changed its actual original work/service')\n    custody = invocation_owner.mint(ProviderInvocationFamily.TITLE)\n    custody.bind_continuation_title(prepared)\n    settings = prepared.composer._settings\n    request = prepared.request\n    from elspeth.web.composer.continuation_bootstrap import bootstrap_values, decode_continuation_bootstrap\n    from elspeth.web.sessions.continuation_title_provider import _chunk\n\n    def original_user() -> str:\n        with locked_session_transaction(prepared.authority._engine, str(prepared.running.claim.session_id)) as conn:\n            _current(conn, prepared.running, prepared.selected)\n            frame = decode_continuation_snapshot(prepared.selected.payload_json).frames[0]\n            if type(frame) is not InitializeFrame or frame.bootstrap is None:\n                raise AuditIntegrityError('TITLE producer lost its original bootstrap')\n            values = bootstrap_values(decode_continuation_bootstrap(_chunk(conn, prepared.running, frame.bootstrap)))\n            original_request = values.request\n            if type(original_request) is not SendMessageRequest:\n                raise AuditIntegrityError('TITLE producer lost its actual original SendMessage request')\n            return original_request.content\n    user = await _registered(work=prepared.work, source=RequiredWorkSource.CONTINUATION_READ_SQL, operation=original_user, expected=str)\n    await _maybe_auto_title_session_required(service=_sessions(prepared.composer), session_id=prepared.running.claim.session_id, model=request.kwargs['model'], temperature=request.kwargs.get('temperature'), seed=request.kwargs.get('seed'), session_operation_context=prepared.running.session_operation_context, kwargs=request.kwargs, audit_messages=request.audit_messages, provider_custody=custody, prompt_guard_mount=prepared.composer.composer_prompt_guard_mount, prompt_guard_mode=settings.composer_prompt_guard_mode, user_message=user, max_prepared_request_bytes=settings.composer_max_prepared_request_bytes, prepared_input=prepared.provider_input)\n    return custody.continuation_title_result",
    "elspeth.web.sessions.composer_turn:_run_composer_turn": "async def _run_composer_turn(services: ComposerAppServices, turn: ComposerTurnInput, *, lease: SessionOperationLease, running: ComposerOperationRunning, request_lifecycle: ComposerRequestLifecycle, observation: ComposerTurnObservation, budget_anchor: ComposerBudgetAnchor) -> ComposerOperationRecord:\n    \"\"\"Run one freeform send or recompose turn to its settled success; see the module docstring.\"\"\"\n    deferred_cancellation: asyncio.CancelledError | None = None\n    continuation_cancellations: list[asyncio.CancelledError] = []\n    auto_title_cancellations: list[asyncio.CancelledError] = []\n    if running.claim.session_id != turn.session_id or running.claim.operation_id != turn.operation_id:\n        raise AuditIntegrityError('composer turn input does not match its running operation')\n    if lease.context != running.session_operation_context:\n        raise AuditIntegrityError(\"composer turn lease is not the running operation's session operation context\")\n    if observation.required_work is None:\n        required_work = lease.required_work\n        observation.required_work = RequiredWorkCoordinator(RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, lease.context, running.claim.operation_id, running.claim.attempt)) if required_work is None else required_work\n    required_work = observation.required_work\n    if type(required_work) is not RequiredWorkCoordinator:\n        raise AuditIntegrityError('Composer turn requires an owned required-work coordinator')\n    lease.bind_required_work(required_work)\n    if turn.budget_seconds != budget_anchor.remaining_at_running_seconds:\n        raise ValueError(\"composer turn budget_seconds is not its budget anchor's remaining_at_running_seconds\")\n    if observation.compose_result is not None or observation.pending_exception_llm_calls or observation.audit_cohort_durable or (observation.compose_base_state_id is not None):\n        raise ValueError('composer turn observation must be fresh for each turn')\n    if request_lifecycle.durable_completed:\n        raise ValueError('composer turn request lifecycle is already durably completed')\n    labels = _SEND_LABELS if turn.kind == 'compose_message' else _RECOMPOSE_LABELS\n    request = turn.request\n    service = services.session_service\n    settings = services.settings\n    provider_binding = RequiredWorkBinding(required_work, 0, 0, RequiredWorkRole.TURN, running)\n    provider_owner = ProviderInvocationOwner(service=service, required_work=provider_binding)\n    try:\n        session = await service.get_session(turn.session_id)\n    except SessionNotFoundError:\n        raise HTTPException(status_code=404, detail='Session not found') from None\n    recovery_admission = await service.load_graph_recovery_context(session.id, turn_start_state_id=request.state_id, session_operation_context=lease.context)\n    recovery_context = recovery_admission.context\n    state_record = recovery_admission.record\n    compose_base_state_id = UUID(recovery_context.turn_start_state_id) if recovery_context.turn_start_state_id is not None else None\n    if compose_base_state_id != request.state_id:\n        raise AuditIntegrityError(f'composer operation {turn.operation_id}: the head under its adopted COMPOSE lease is not the bound base the start composite verified')\n    state = None if recovery_context.malformed_head else _initial_composition_state() if state_record is None else _state_from_record(state_record)\n    prior_completion_gates_facts = recovery_admission.effective_completion_gates if recovery_admission.effective_completion_gates is not None else parse_completion_gates(state_record.composer_meta) if state_record is not None else None\n    plugin_snapshot = services.plugin_snapshot_for_user_id(turn.actor_user_id)\n    profile_registry = services.operator_profile_registry\n    if isinstance(request, SendMessageRequest):\n        message_text = request.content\n        ingress_sql, ingress_projection = observation.required_work.reserve_pair(RequiredWorkSource.INGRESS_SQL, RequiredWorkSource.INGRESS_PROJECTION, transition_ordinal=0, semantic_ordinal=0)\n        ingress = lease.create_task(_capture_freeform_child(service.add_message_with_transcript(session.id, 'user', request.content, operation_id=UUID(turn.operation_id), requested_state_id=request.state_id, composition_state_id=compose_base_state_id, writer_principal='route_user_message', session_operation_context=lease.context, running=running, required_work=ingress_sql)), name='send-message-ingress')\n        try:\n            ingress_outcome, ingress_cancellation = await _join_freeform_owned_task(ingress)\n            ingress_result = _freeform_child_result(ingress_outcome)\n        except BaseException:\n            ingress_projection.complete_owned()\n            raise\n        try:\n            if not isinstance(ingress_result, MessageIngressFresh):\n                raise AuditIntegrityError(f'composer operation {turn.operation_id}: message ingress was not fresh for a running job')\n            turn_user_message_id = ingress_result.message.id\n            records = list(ingress_result.transcript)\n        except BaseException as exc:\n            ingress_projection.complete_owned(exc)\n            raise\n        ingress_projection.complete_owned()\n        if ingress_cancellation is not None:\n            raise ingress_cancellation\n    else:\n        records = await service.get_messages(session.id, limit=None, include_private_evidence=False)\n        recompose_conversation = _composer_conversation_messages(records)\n        latest_user = next((row for row in reversed(recompose_conversation) if row.role == 'user'), None)\n        if latest_user is None or latest_user.id != request.expected_user_message_id:\n            raise AuditIntegrityError(f'composer operation {turn.operation_id}: the recompose transcript changed under its adopted COMPOSE lease after the start composite verified it')\n        latest_user_index = recompose_conversation.index(latest_user)\n        if any((row.role == 'assistant' and (not row.tool_calls) for row in recompose_conversation[latest_user_index + 1:])):\n            raise AuditIntegrityError('Recompose saved response appeared under its exact COMPOSE lease')\n        proposals = await service.list_composition_proposals(session.id)\n        if any((p.user_message_id == latest_user.id and p.pipeline_metadata is not None and (p.status in ('pending', 'committed')) for p in proposals)):\n            raise AuditIntegrityError('Recompose saved proposal appeared under its exact COMPOSE lease')\n        message_text = latest_user.content\n        turn_user_message_id = latest_user.id\n    chat_ingress = compartment_ingress_record(message_text, own_compartment_id=settings.compartment_id)\n    chat_ingress_inputs = _chat_ingress_inputs(records, own_compartment_id=settings.compartment_id)\n    progress_sink = await _advisory_progress_sink(services, request_lifecycle=request_lifecycle, session_id=session.id, operation_id=turn.operation_id, running=running, request_id=str(turn_user_message_id), user_id=turn.actor_user_id)\n    await _publish_progress(progress_sink, event=ComposerProgressEvent(phase='starting', headline=labels.starting_headline, evidence=(labels.starting_evidence,), likely_next='ELSPETH will prepare the composer prompt with the current pipeline.'))\n    _COMPOSER_REQUESTS_INFLIGHT.add(1, {'endpoint': labels.endpoint})\n    terminal_status: _ComposerRequestTerminalStatus = 'failed'\n    try:\n        if isinstance(request, SendMessageRequest):\n            send_conversation = _composer_conversation_messages(records)\n            if not send_conversation or send_conversation[-1].id != turn_user_message_id:\n                raise AuditIntegrityError(f'Tier 1 audit anomaly: send_message transcript snapshot for session {session.id} does not end at inserted user message {turn_user_message_id}. Refusing to compose against interleaved session history.')\n        has_partial_turn = isinstance(request, RecomposeRequest) and any((row.role == 'assistant' for row in recompose_conversation[latest_user_index + 1:]))\n        chat_messages = _composer_chat_history(records if has_partial_turn else [record for record in records if record.id != turn_user_message_id])\n        should_auto_title = not recovery_context.malformed_head and isinstance(request, SendMessageRequest) and (len(records) == 1) and is_default_session_title(session.title)\n        from openai import OpenAIError\n\n        async def settle_post_provider(result: ComposerResult | GraphRecoveryResponse) -> ComposerOperationRecord:\n            return await settle_composer_result(ComposerFinalizationInputs(service=service, session=session, recovery_context=recovery_context, lease=lease, compose_base_state_id=compose_base_state_id, running=running, observation=observation, progress_sink=progress_sink, state=state, chat_ingress=chat_ingress, chat_ingress_inputs=chat_ingress_inputs, prior_completion_gates_facts=prior_completion_gates_facts, state_record=state_record, budget_anchor=budget_anchor, services=services, turn=turn, labels=labels, provider_binding=provider_binding, settings=settings, plugin_snapshot=plugin_snapshot, profile_registry=profile_registry, required_work=required_work), result)\n        history_task = lease.create_task(_capture_retained_operation_read(_read_retained_operation_context(authority=services.composer_operation_authority, binding=provider_binding, session_id=turn.session_id, actor_user_id=turn.actor_user_id)), name='composer-retained-operation-history')\n        history_cancellations: list[asyncio.CancelledError] = []\n        history_outcome, _ = await _join_freeform_owned_task(history_task, cancellation_observations=history_cancellations)\n        if type(history_outcome) is not _RetainedOperationReadOutcome:\n            raise AuditIntegrityError('Retained history child lost its actual outcome')\n        if history_outcome.original_error is not None:\n            history_failure = history_outcome.original_error\n            if history_cancellations:\n                raise BaseExceptionGroup('Retained history failure and caller cancellation', [history_failure, *history_cancellations]) from history_failure\n            raise history_failure\n        if len(history_cancellations) == 1:\n            raise history_cancellations[0]\n        if history_cancellations:\n            raise BaseExceptionGroup('Retained history caller cancellations', history_cancellations)\n        retained_operation_context = history_outcome.context\n        if retained_operation_context is None:\n            raise AuditIntegrityError('Retained history success lacks its actual projected context')\n        provider_owner.bind_cost_budget(capture_common_provider_cost(session_id=str(session.id), request_id=str(turn_user_message_id), lifecycle_version=1, initially_empty=state is not None and state_is_structurally_empty(state), settings=settings))\n        initial_preferences = await service.get_composer_preferences(turn.session_id)\n        mutation_ceiling = ComposerMutationCeiling(str(turn.session_id), str(turn_user_message_id), initial_preferences.trust_mode)\n        composer = services.composer_service\n        post_provider_error: BaseException | None = None\n        settled_record: ComposerOperationRecord | None = None\n        compose_budget_seconds = budget_anchor.remaining_seconds(monotonic_now=time.monotonic())\n        observation.compose_base_state_id = compose_base_state_id\n        if compose_budget_seconds <= 0:\n            terminal_status = 'timed_out'\n            await _publish_progress(progress_sink, event=convergence_progress_event(budget_exhausted='timeout'))\n            raise ComposerTurnDeadlineExpired(session_id=turn.session_id, operation_id=turn.operation_id, remaining_seconds=compose_budget_seconds, budget_seconds_at_running=budget_anchor.remaining_at_running_seconds)\n        try:\n            if should_auto_title:\n                title_custody = provider_owner.mint(ProviderInvocationFamily.TITLE)\n                title_producer = required_work.reserve(RequiredWorkSource.TITLE_CONTINUATION_PRODUCER, transition_ordinal=title_custody.required_work.transition_ordinal, semantic_ordinal=title_custody.required_work.semantic_ordinal)\n\n                async def run_owned_title() -> None:\n                    try:\n                        await maybe_auto_title_session(service=service, session_id=session.id, user_message=message_text, model=settings.composer_model, temperature=settings.composer_temperature, seed=settings.composer_seed, session_operation_context=lease.context, prompt_guard_mount=services.composer_service.composer_prompt_guard_mount, prompt_guard_mode=settings.composer_prompt_guard_mode, max_prepared_request_bytes=settings.composer_max_prepared_request_bytes, api_base=settings.composer_endpoint_base_url, api_key=settings.composer_endpoint_api_key.get_secret_value() if settings.composer_endpoint_api_key is not None else None, provider_custody=title_custody)\n                    except BaseException as failure:\n                        title_producer.complete_owned(failure)\n                        raise\n                    title_producer.complete_owned()\n                try:\n                    auto_title_task = lease.create_task(run_owned_title())\n                except BaseException as failure:\n                    title_producer.complete_owned(failure)\n                    raise\n                await _join_auto_title(auto_title_task, session_id=turn.session_id, operation_id=turn.operation_id, cancellation_observations=auto_title_cancellations, wait_until_complete=True)\n                compose_budget_seconds = budget_anchor.remaining_seconds(monotonic_now=time.monotonic())\n                if compose_budget_seconds <= 0:\n                    terminal_status = 'timed_out'\n                    raise ComposerTurnDeadlineExpired(session_id=turn.session_id, operation_id=turn.operation_id, remaining_seconds=compose_budget_seconds, budget_seconds_at_running=budget_anchor.remaining_at_running_seconds)\n            result: ComposerResult | GraphRecoveryResponse\n            if recovery_context.malformed_head:\n                result = await composer.compose_recovery(message=message_text, recovery_context=recovery_context, session_operation_context=lease.context, budget_seconds=compose_budget_seconds, required_work=provider_binding, provider_owner=provider_owner)\n            else:\n                if state is None:\n                    raise AuditIntegrityError('Ordinary Composer admission lacks its graph')\n                result = await composer.compose(message_text, chat_messages, state, session_id=str(turn.session_id), current_state_id=str(compose_base_state_id) if compose_base_state_id is not None else None, user_id=turn.actor_user_id, progress=progress_sink, session_operation_context=lease.context, user_message_id=str(turn_user_message_id), mutation_ceiling=mutation_ceiling, completion_gates=prior_completion_gates_facts, budget_seconds=compose_budget_seconds, required_work=provider_binding, provider_owner=provider_owner, recovery_context=recovery_context, retained_operation_context=retained_operation_context, advisor_history=AdvisorTranscriptHistory.from_records(session_id=str(turn.session_id), actor_user_id=turn.actor_user_id, current_request_id=str(turn_user_message_id), request=message_text, records=records))\n            observation.compose_result = result\n            continuation = lease.create_task(_capture_freeform_child(settle_post_provider(result)), name=labels.settlement_task_name)\n            try:\n                outcome, deferred_cancellation = await _join_freeform_owned_task(continuation, cancellation_observations=continuation_cancellations)\n                receipt = _freeform_child_result(outcome)\n            except (AuditIntegrityError, SQLAlchemyError, HTTPException, InvariantError, ComposerConvergenceError, ComposerRuntimePreflightError, ComposerPluginCrashError, ComposerServiceError, PipelinePlannerError, ChargeableAdmissionRefused, ComposerAdmissionRefused, OpenAIError, _BadRequestLLMError, ComposerOperationCancelledDuringTurn, ComposerOperationFenceLost, SessionOperationFenceLost, AsyncWorkerAdmissionTimeoutError, ComposerTurnDeadlineExpired, asyncio.CancelledError) as exc:\n                post_provider_error = exc\n            except Exception as exc:\n                post_provider_error = ComposerOwnedSettlementFailure()\n                post_provider_error.__cause__ = exc\n            else:\n                settled_record = receipt\n                request_lifecycle.durable_completed = True\n                terminal_status = 'completed'\n        except asyncio.CancelledError as turn_cancel:\n            if post_provider_error is not None:\n                raise post_provider_error from turn_cancel\n            raise\n        except ComposerToolFailure as failure:\n            if type(failure) is not ComposerToolFailure:\n                raise\n            observation.pending_exception_tool_invocations = failure.tool_invocations\n            observation.pending_exception_llm_calls = failure.llm_calls\n            await _required_audit(observation, _persist_turn_audit_cohort(service, session.id, failure.tool_invocations, failure.llm_calls, tool_composition_state_id=compose_base_state_id, llm_composition_state_id=compose_base_state_id, plugin_crash_pending=True, required_audit=True, required_work=observation.required_work, session_operation_context=lease.context))\n            observation.audit_cohort_durable = True\n            post_provider_error = failure.original_exception\n        except ComposerConvergenceError as exc:\n            observation.pending_exception_llm_calls = _llm_calls_from_exception(exc)\n            terminal_status = 'timed_out' if exc.budget_exhausted == 'timeout' else 'failed'\n            await _publish_progress(progress_sink, event=convergence_progress_event(budget_exhausted=exc.budget_exhausted))\n            observation.pending_exception_tool_invocations = exc.tool_invocations if exc.failed_turn is None else ()\n            response_body = await _required_audit(observation, _handle_convergence_error(exc, service, session.id, turn.actor_user_id, labels.convergence_log_prefix, compose_base_state_id, settings=settings, secret_service=services.scoped_secret_resolver, plugin_snapshot=plugin_snapshot, profile_registry=profile_registry, catalog=services.catalog_service, session_operation_context=lease.context, ingress=chat_ingress, chat_ingress_inputs=chat_ingress_inputs, budget_seconds=compose_budget_seconds, required_audit=True, required_work=required_work))\n            observation.audit_cohort_durable = True\n            raise HTTPException(status_code=422, detail=response_body) from exc\n        except OpenAIError as exc:\n            observation.pending_exception_llm_calls = _llm_calls_from_exception(exc)\n            provider_failure = await _required_audit(observation, _handle_composer_provider_failure(exc, route=labels.provider_route, service=service, session_id=session.id, composition_state_id=compose_base_state_id, progress_sink=progress_sink, session_operation_context=lease.context, expose_provider_error=settings.composer_expose_provider_errors, required_audit=True, required_work=observation.required_work))\n            observation.audit_cohort_durable = True\n            raise provider_failure from exc\n        except _BadRequestLLMError as exc:\n            llm_calls = _llm_calls_from_exception(exc)\n            observation.pending_exception_llm_calls = llm_calls\n            slog.error(labels.llm_bad_request_event, session_id=str(turn.session_id), exc_class=type(exc).__name__)\n            await _publish_progress(progress_sink, event=ComposerProgressEvent(phase='failed', headline=f'The composer model rejected this {labels.noun}.', evidence=('The model provider rejected the composer request as invalid.',), likely_next='Check the composer provider configuration and request options before retrying.', reason='provider_unavailable'))\n            if llm_calls:\n                await _required_audit(observation, _persist_llm_calls(service, session.id, llm_calls, compose_base_state_id, plugin_crash_pending=True, session_operation_context=lease.context, required_audit=True, required_work=observation.required_work))\n                observation.audit_cohort_durable = True\n            raise HTTPException(status_code=502, detail=_litellm_error_detail('llm_unavailable', exc, expose_provider_error=settings.composer_expose_provider_errors)) from exc\n        except ComposerPluginCrashError as crash:\n            observation.pending_exception_llm_calls = _llm_calls_from_exception(crash)\n            observation.pending_exception_tool_invocations = crash.tool_invocations if crash.failed_turn is None else ()\n            response_body = await _required_audit(observation, _handle_plugin_crash(crash, service, session.id, turn.actor_user_id, labels.handler_log_prefix, compose_base_state_id, settings=settings, secret_service=services.scoped_secret_resolver, plugin_snapshot=plugin_snapshot, profile_registry=profile_registry, catalog=services.catalog_service, session_operation_context=lease.context, ingress=chat_ingress, chat_ingress_inputs=chat_ingress_inputs, required_audit=True, required_work=required_work))\n            observation.audit_cohort_durable = True\n            await _publish_progress(progress_sink, event=ComposerProgressEvent(phase='failed', headline=f'The composer could not safely finish this {labels.noun}.', evidence=('A pipeline tool failed on the server side.',), likely_next='Review the visible error message, then retry after the issue is resolved.', reason='plugin_crash'))\n            raise HTTPException(status_code=500, detail=response_body) from crash.original_exc\n        except ComposerRuntimePreflightError as rpf_exc:\n            observation.pending_exception_llm_calls = _llm_calls_from_exception(rpf_exc)\n            _record_composer_runtime_preflight_telemetry('exception', source='cached_preflight', exception_class=rpf_exc.exc_class)\n            await _publish_progress(progress_sink, event=ComposerProgressEvent(phase='failed', headline=f'The composer could not safely finish this {labels.noun}.', evidence=('Runtime preflight failed before the compose loop returned.',), likely_next='Review the visible error message, then retry after the issue is resolved.', reason='runtime_preflight_failed'))\n            observation.pending_exception_tool_invocations = rpf_exc.tool_invocations\n            response_body = await _required_audit(observation, _handle_runtime_preflight_failure(rpf_exc, service, session.id, turn.actor_user_id, labels.handler_log_prefix, compose_base_state_id, settings=settings, secret_service=services.scoped_secret_resolver, plugin_snapshot=plugin_snapshot, profile_registry=profile_registry, catalog=services.catalog_service, session_operation_context=lease.context, ingress=chat_ingress, chat_ingress_inputs=chat_ingress_inputs, required_audit=True, required_work=required_work))\n            observation.audit_cohort_durable = True\n            raise HTTPException(status_code=500, detail=response_body) from rpf_exc.original_exc\n        except (PipelinePlannerError, CommonProviderCostRefused) as exc:\n            observation.pending_exception_llm_calls = _llm_calls_from_exception(exc)\n            if isinstance(exc, CommonProviderCostRefused) and observation.pending_exception_llm_calls:\n                await _required_audit(observation, _persist_llm_calls(service, session.id, observation.pending_exception_llm_calls, compose_base_state_id, plugin_crash_pending=False, session_operation_context=lease.context, required_audit=True, required_work=observation.required_work))\n                observation.audit_cohort_durable = True\n                observation.pending_exception_llm_calls = ()\n            await _publish_progress(progress_sink, event=ComposerProgressEvent(phase='failed', headline=f'The composer could not build a pipeline for this {labels.noun}.', evidence=('Cost accounting could not admit the model response.' if exc.code == 'COST_UNAVAILABLE' else 'The composer model did not return a usable pipeline plan.',), likely_next='Ask an administrator to configure or correct model pricing before trying again.' if exc.code == 'COST_UNAVAILABLE' else 'Retry the request; if it keeps failing, simplify it or check the composer provider.', reason=freeform_planner_progress_reason(exc.code)))\n            status_code, planner_response_body = await _required_audit(observation, _handle_planner_failure(exc, service, session.id, compose_base_state_id, session_operation_context=lease.context, required_audit=True, required_work=observation.required_work))\n            raise HTTPException(status_code=status_code, detail=planner_response_body) from exc\n        except ChargeableAdmissionRefused as exc:\n            observation.pending_exception_llm_calls = _llm_calls_from_exception(exc)\n            chargeable_refusal = await _required_audit(observation, _handle_composer_chargeable_refusal(exc, service=service, session_id=session.id, composition_state_id=compose_base_state_id, progress_sink=progress_sink, session_operation_context=lease.context, required_audit=True, required_work=observation.required_work))\n            observation.audit_cohort_durable = True\n            raise chargeable_refusal from exc\n        except PromptGuardRefused as exc:\n            llm_calls = exc.llm_calls\n            observation.pending_exception_llm_calls = llm_calls\n            observation.pending_exception_tool_invocations = exc.tool_invocations\n            if llm_calls or exc.tool_invocations:\n                await _required_audit(observation, _persist_turn_audit_cohort(service, session.id, exc.tool_invocations, llm_calls, tool_composition_state_id=compose_base_state_id, llm_composition_state_id=compose_base_state_id, plugin_crash_pending=True, session_operation_context=lease.context, required_audit=True, required_work=observation.required_work))\n                observation.audit_cohort_durable = True\n            raise HTTPException(status_code=exc.http_status, detail=exc.to_payload()) from exc\n        except ComposerContextUnsupported as exc:\n            llm_calls = exc.llm_calls\n            observation.pending_exception_llm_calls = llm_calls\n            observation.pending_exception_tool_invocations = exc.tool_invocations\n            if llm_calls or exc.tool_invocations:\n                await _required_audit(observation, _persist_turn_audit_cohort(service, session.id, exc.tool_invocations, llm_calls, tool_composition_state_id=compose_base_state_id, llm_composition_state_id=compose_base_state_id, plugin_crash_pending=True, session_operation_context=lease.context, required_audit=True, required_work=observation.required_work))\n                observation.audit_cohort_durable = True\n            await _publish_progress(progress_sink, event=ComposerProgressEvent(phase='failed', headline=\"The model cannot review this request's complete context.\", evidence=(exc.to_payload()['detail'],), likely_next='Review the pipeline and request size before sending again.', reason='context_unsupported'))\n            raise HTTPException(status_code=exc.http_status, detail=exc.to_payload()) from exc\n        except ComposerAdmissionRefused as exc:\n            observation.pending_exception_llm_calls = _llm_calls_from_exception(exc)\n            await _publish_progress(progress_sink, event=ComposerProgressEvent(phase='failed', headline='This request was refused by the admission policy.', evidence=(str(exc),), likely_next='Ask an administrator to review your access and quota configuration.', reason='admission_refused'))\n            if isinstance(exc, CredentialMaterialRefused):\n                raise HTTPException(status_code=422, detail=exc.to_payload()) from exc\n            raise HTTPException(status_code=403, detail={'error_type': 'composer_admission_refused', 'failure_code': 'admission_refused', 'detail': str(exc)}) from exc\n        except ComposerMutationAuthorityRevoked as exc:\n            observation.pending_exception_llm_calls = _llm_calls_from_exception(exc)\n            await _publish_progress(progress_sink, event=ComposerProgressEvent(phase='failed', headline='Automatic mutation authority was revoked.', evidence=(str(exc),), likely_next='Review the saved session before retrying.', reason='mutation_authority_revoked'))\n            raise HTTPException(status_code=409, detail=exc.to_payload()) from exc\n        except ComposerServiceError as exc:\n            llm_calls = _llm_calls_from_exception(exc)\n            observation.pending_exception_llm_calls = llm_calls\n            await _publish_progress(progress_sink, event=ComposerProgressEvent(phase='failed', headline=f'The composer could not finish this {labels.noun}.', evidence=('Prompt preparation or composer service setup failed.',), likely_next='Retry once the composer service is available.', reason='service_setup_failed'))\n            if llm_calls:\n                await _required_audit(observation, _persist_llm_calls(service, session.id, llm_calls, compose_base_state_id, plugin_crash_pending=True, session_operation_context=lease.context, required_audit=True, required_work=observation.required_work))\n                observation.audit_cohort_durable = True\n            raise HTTPException(status_code=502, detail={'error_type': 'composer_error', 'detail': str(exc)}) from exc\n        finally:\n            current_exc = sys.exception()\n            drained_calls = () if current_exc is None or isinstance(current_exc, asyncio.CancelledError) else _llm_calls_from_exception(current_exc)\n            if drained_calls:\n                observation.pending_exception_llm_calls = drained_calls\n                await _required_audit(observation, _persist_llm_calls(service, session.id, drained_calls, compose_base_state_id, plugin_crash_pending=True, session_operation_context=lease.context, required_audit=True, required_work=observation.required_work))\n                observation.audit_cohort_durable = True\n        if post_provider_error is not None:\n            if type(post_provider_error) is ComposerTurnDeadlineExpired and continuation_cancellations:\n                deadline_outcomes: list[BaseException] = [post_provider_error]\n                for cancellation in continuation_cancellations:\n                    if all((cancellation is not prior for prior in deadline_outcomes)):\n                        deadline_outcomes.append(cancellation)\n                raise BaseExceptionGroup('Composer deadline retained joined caller cancellations', deadline_outcomes)\n            if type(post_provider_error) is ComposerOwnedSettlementFailure and continuation_cancellations:\n                original_cause = post_provider_error.__cause__\n                secondary: list[BaseException] = []\n                if original_cause is not None and original_cause is not post_provider_error:\n                    secondary.append(original_cause)\n                for cancellation in continuation_cancellations:\n                    if all((cancellation is not prior for prior in secondary)):\n                        secondary.append(cancellation)\n                if secondary:\n                    raise post_provider_error from BaseExceptionGroup('Composer settlement retained original cause and joined caller cancellations', secondary)\n            raise post_provider_error\n        if settled_record is None:\n            raise InvariantError('Provider returned without a freeform continuation receipt')\n        if deferred_cancellation is not None:\n            raise deferred_cancellation\n        if observation.deferred_cancellation is not None:\n            raise observation.deferred_cancellation\n        return settled_record\n    except ComposerRequiredAuditPersistenceError as exc:\n        _COMPOSER_TIER1_VIOLATION_COUNTER.add(1, {'helper': exc.helper})\n        raise\n    except InvariantError as exc:\n        slog.error('composer.invariant_violated', session_id=str(turn.session_id), user_id=turn.actor_user_id, exc_class=type(exc).__name__, site=labels.site, frames=_safe_frame_strings(exc))\n        original_outcomes: list[BaseException] = [exc]\n        for cancelled in (*continuation_cancellations, deferred_cancellation, observation.deferred_cancellation):\n            if cancelled is not None and all((cancelled is not original for original in original_outcomes)):\n                original_outcomes.append(cancelled)\n        cause = exc if len(original_outcomes) == 1 else BaseExceptionGroup('Invariant failure retained original deferred cancellation', original_outcomes)\n        raise HTTPException(status_code=500, detail={'error_type': 'server_invariant_violated', 'detail': 'Server invariant violated. See application audit log for diagnostic detail.'}) from cause\n    except asyncio.CancelledError as exc:\n        if request_lifecycle.durable_completed:\n            terminal_status = 'completed'\n            if len(auto_title_cancellations) > 1:\n                raise BaseExceptionGroup('Auto-title join retained caller cancellations', auto_title_cancellations) from exc\n            raise\n        tool_invocations = exc.tool_invocations if type(exc) is ComposerToolCancellation else ()\n        llm_calls = exc.llm_calls if type(exc) is ComposerToolCancellation else _llm_calls_from_exception(exc)\n        observation.pending_exception_llm_calls = llm_calls\n        observation.pending_exception_tool_invocations = tool_invocations\n        try:\n            if tool_invocations or llm_calls:\n                await _join_shielded_task_after_cancellation(asyncio.create_task(_persist_turn_audit_cohort(service, session.id, tool_invocations, llm_calls, tool_composition_state_id=compose_base_state_id, llm_composition_state_id=compose_base_state_id, plugin_crash_pending=True, session_operation_context=lease.context, required_audit=True, required_work=observation.required_work), name=labels.cancelled_llm_task_name), primary_cancellation=exc)\n                observation.audit_cohort_durable = True\n            user_stop = _cancel_is_user_stop(exc.original_cancellation if type(exc) is ComposerToolCancellation else exc)\n            await _join_shielded_task_after_cancellation(asyncio.create_task(_publish_progress(progress_sink, event=client_cancelled_progress_event() if user_stop else _composer_heartbeat_failed_progress_event()), name=labels.cancelled_progress_task_name), primary_cancellation=exc)\n        except BaseException as cleanup_failure:\n            if len(auto_title_cancellations) > 1:\n                raise BaseExceptionGroup('Composer cancellation cleanup retained auto-title caller cancellations', [cleanup_failure, *auto_title_cancellations]) from cleanup_failure\n            raise\n        terminal_status = 'cancelled' if user_stop else 'failed'\n        if len(auto_title_cancellations) > 1:\n            raise BaseExceptionGroup('Auto-title join retained caller cancellations', auto_title_cancellations) from exc\n        raise\n    finally:\n        _COMPOSER_REQUESTS_INFLIGHT.add(-1, {'endpoint': labels.endpoint})\n        _record_composer_request_terminal(terminal_status, endpoint=labels.endpoint)",
    "elspeth.web.composer.service:ComposerServiceImpl._new_primary_custody": "def _new_primary_custody(self, provider_owner: ProviderInvocationOwner | None) -> ProviderCallCustody | None:\n    if provider_owner is None:\n        return None\n    if type(provider_owner) is not ProviderInvocationOwner or provider_owner.service is not self._require_sessions_service():\n        raise AuditIntegrityError('Primary invocation owner belongs to another exact service')\n    return provider_owner.mint(ProviderInvocationFamily.PRIMARY)",
    "elspeth.web.composer.advisor_checkpoint:AdvisorCheckpointOwner._new_advisor_custody": "def _new_advisor_custody(self, provider_owner: ProviderInvocationOwner) -> ProviderCallCustody:\n    if type(provider_owner) is not ProviderInvocationOwner or provider_owner.service is not self._require_sessions_service():\n        raise AuditIntegrityError('Advisor invocation owner belongs to another exact service')\n    return provider_owner.mint(ProviderInvocationFamily.ADVISOR)",
    "elspeth.web.execution.routes:create_execution_router.evaluate_run_diagnostics": "@router.post('/api/runs/{run_id}/diagnostics/evaluate', response_model=RunDiagnosticsEvaluationResponse)\nasync def evaluate_run_diagnostics(run_id: UUID, request: Request, user: Annotated[UserIdentity, Depends(require_pipeline_user)], limit: int=Query(50, ge=1, le=100), service: ExecutionService=Depends(_get_execution_service)) -> RunDiagnosticsEvaluationResponse:\n    \"\"\"Ask the configured LLM to explain the current diagnostics snapshot.\"\"\"\n    run = await _verify_run_ownership(run_id, user, request)\n    rate_limiter = await get_rate_limiter(request)\n    await rate_limiter.check(user.user_id)\n    try:\n        status = await _load_run_status_with_accounting(run_id, app=request.app, service=service)\n    except _RunStatusNotFoundError:\n        raise _run_not_found_http() from None\n    except (ValidationError, _RunStatusIntegrityError) as exc:\n        raise _run_integrity_http(exc) from exc\n    try:\n        diagnostics = await run_sync_in_worker(load_run_diagnostics_for_settings, request.app.state.settings, run_id=status.run_id, landscape_run_id=status.landscape_run_id, run_status=status.status, cancel_requested=status.cancel_requested, limit=limit)\n    except RunDiagnosticsAuditUnavailableError as exc:\n        raise HTTPException(status_code=503, detail={'error_type': 'run_diagnostics_audit_unavailable', 'landscape_run_id': exc.landscape_run_id, 'audit_location': exc.audit_location}) from exc\n    composer: ComposerService = request.app.state.composer_service\n    settings: WebSettings = request.app.state.settings\n    recorder = BufferingRecorder()\n    audit_authority = RunDiagnosticsAuditAuthority(run_id=run.id, session_id=run.session_id, state_id=run.state_id)\n\n    async def _persist_diagnostics_llm_calls(*, plugin_crash_pending: bool) -> None:\n        compose_lock = await _get_session_compose_lock_registry(request).get_lock(str(run.session_id))\n        async with compose_lock:\n            await _persist_run_diagnostics_llm_calls(request.app.state.session_service, audit_authority, recorder.llm_calls, plugin_crash_pending=plugin_crash_pending)\n    try:\n        session_service: SessionServiceProtocol = request.app.state.session_service\n        async with await SessionOperationLease.acquire(session_service.session_operation_authority, session_id=run.session_id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id=session_service.session_operation_owner_instance_id, lease_seconds=session_service.session_operation_lease_seconds) as diagnostics_lease:\n            coordinator = RequiredWorkCoordinator(RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, diagnostics_lease.context, invocation_id=str(uuid4())))\n            owner = ProviderInvocationOwner(service=session_service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))\n            try:\n                explanation = await composer.explain_run_diagnostics(llm_safe_diagnostics_snapshot(diagnostics), recorder=recorder, session_operation_context=diagnostics_lease.context, provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY))\n            finally:\n                coordinator.assert_completed()\n    except PromptGuardRefused as exc:\n        await _persist_diagnostics_llm_calls(plugin_crash_pending=True)\n        raise HTTPException(status_code=exc.http_status, detail=exc.to_payload()) from exc\n    except _BadRequestLLMError as exc:\n        await _persist_diagnostics_llm_calls(plugin_crash_pending=True)\n        raise HTTPException(status_code=502, detail=_litellm_error_detail('run_diagnostics_explanation_failed', exc, expose_provider_error=settings.composer_expose_provider_errors)) from exc\n    except ComposerContextUnsupported as exc:\n        await _persist_diagnostics_llm_calls(plugin_crash_pending=False)\n        raise HTTPException(status_code=exc.http_status, detail=exc.to_payload()) from exc\n    except ComposerAdmissionRefused as exc:\n        if isinstance(exc, CredentialMaterialRefused):\n            raise HTTPException(status_code=422, detail=exc.to_payload()) from exc\n        raise HTTPException(status_code=403, detail={'error_type': 'composer_admission_refused', 'failure_code': 'admission_refused', 'detail': str(exc)}) from exc\n    except ComposerServiceError as exc:\n        await _persist_diagnostics_llm_calls(plugin_crash_pending=True)\n        raise HTTPException(status_code=502, detail={'error_type': 'run_diagnostics_explanation_failed', 'detail': str(exc)}) from exc\n    except BaseException:\n        await _persist_diagnostics_llm_calls(plugin_crash_pending=True)\n        raise\n    else:\n        try:\n            await _persist_diagnostics_llm_calls(plugin_crash_pending=False)\n        except RunDiagnosticsAuthorityLostError as lost:\n            raise HTTPException(status_code=409, detail={'error_type': 'run_diagnostics_audit_authority_lost', 'detail': f'Run diagnostics audit authority lost ({lost.reason}); the explanation was discarded because its audit row could not be written.'}) from lost\n    explanation, working_view = _parse_run_diagnostics_working_view(explanation, diagnostics)\n    return RunDiagnosticsEvaluationResponse(run_id=status.run_id, generated_at=datetime.now(UTC), explanation=explanation, working_view=working_view)",
    "elspeth.web.sessions.continuation_driver:_SegmentDriver.__init__": "def __init__(self, worker: ComposerAsyncWorker, services: ComposerAppServices, running: ComposerOperationRunning, lease: SessionOperationLease):\n    if type(services.composer_service) is not ComposerServiceImpl or type(lease.required_work) is not RequiredWorkCoordinator:\n        raise AuditIntegrityError('V2 driver requires its actual Composer and required-work owners')\n    if lease.context is not running.session_operation_context:\n        raise AuditIntegrityError('V2 driver changed its exact started context')\n    self.worker, self.services, self.running, self.lease = (worker, services, running, lease)\n    self.service = services.composer_service\n    self.authority = services.composer_operation_authority\n    self.work = RequiredWorkBinding(lease.required_work, 0, 0, RequiredWorkRole.TURN, running)\n    self.provider = ProviderInvocationOwner(service=services.session_service, required_work=self.work)\n    self.phases = ContinuationLivePhases(self.service, self.authority, self.work, self.provider, lease)\n    sql_deadline = lease.required_work.sql_deadline_owner\n    if sql_deadline is None:\n        raise AuditIntegrityError('V2 driver lost its actual SQL timing owner')\n    self.sql_deadline = sql_deadline\n    self._consumed_native: list[BaseException] = []\n    self._committed_failure: CommittedContinuationFailure | None = None",
}


QUOTA = "elspeth.web.composer.provider_quota"
OWNER = QUOTA + ".ProviderInvocationOwner"
CUSTODY = QUOTA + ".ProviderCallCustody"
FAMILY = QUOTA + ".ProviderInvocationFamily"
BINDING = "elspeth.web.required_work.RequiredWorkBinding"
TITLE = "elspeth.web.composer.continuation_title_provider"
DRIVER = "elspeth.web.sessions.continuation_driver"
DRIVER_CLASS_HEADER = "class _SegmentDriver:\n\n    def __init__(self, worker: ComposerAsyncWorker, services: ComposerAppServices, running: ComposerOperationRunning, lease: SessionOperationLease):\n        pass\n\n    def join_failure(self, error: BaseException, *additional: BaseException) -> BaseException:\n        pass\n\n    async def record(self) -> ComposerOperationRecord:\n        pass\n\n    async def selected(self) -> tuple[StoredContinuationSnapshot, SelectedContinuationState]:\n        pass\n\n    async def settle(self, record: ComposerOperationRecord, original: BaseException | None=None) -> ContinuationSegmentSettled:\n        pass\n\n    async def read_failure(self, names: frozenset[str]) -> ContinuationToolFailureRead:\n        pass\n\n    async def dispose(self, original: BaseException | None=None) -> ContinuationSegmentOutcome:\n        pass\n\n    async def advance(self, stored: StoredContinuationSnapshot, selected: SelectedContinuationState) -> MessageWithStateResponse | None:\n        pass\n\n    async def watch_stage(self, stage: asyncio.Task[MessageWithStateResponse | None], reader: ComposerOperationWatchReader) -> None:\n        pass\n\n    async def run(self, adoption_failure: BaseException | None) -> ContinuationSegmentOutcome:\n        pass"


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
                if (unit.path, id(node)) in checked and node.name in {
                    "_new_primary_custody",
                    "_new_advisor_custody",
                }:
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
    return [], {
        "owner_constructor_stores": frozenset({(driver.path, id(store))}),
        "owner_constructor_calls": frozenset({(driver.path, id(store.value))}),
        "caller_regions": frozenset({(driver.path, id(init))}),
        "class_origins": frozenset({DRIVER + "._SegmentDriver"}),
    }


def provider_mint_local_contracts(units):
    units = tuple(units)
    driver_failures, driver = provider_driver_constructor_contract(units)
    if driver_failures:
        return driver_failures, None
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
        "elspeth.web.sessions.composer_turn": {
            "ProviderInvocationOwner": OWNER,
            "ProviderInvocationFamily": FAMILY,
            "ComposerContextUnsupported": "elspeth.web.composer.protocol.ComposerContextUnsupported",
            "capture_common_provider_cost": "elspeth.web.composer.provider_cost.capture_common_provider_cost",
            "CommonProviderCostRefused": "elspeth.web.composer.provider_cost.CommonProviderCostRefused",
            "state_is_structurally_empty": "elspeth.web.composer.no_tool_policy.state_is_structurally_empty",
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
            "_run_composer_turn",
            "ComposerServiceImpl._new_primary_custody",
            "AdvisorCheckpointOwner._new_advisor_custody",
            "create_execution_router.evaluate_run_diagnostics",
            "run_prepared_title_provider",
            "_SegmentDriver.__init__",
        }
    ]
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
