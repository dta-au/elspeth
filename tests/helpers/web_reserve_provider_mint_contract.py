"""Finite actual provider owner/mint/caller transfer source contract."""

import ast

CONTRACTS = {
    "elspeth.web.composer.provider_quota:ProviderInvocationFamily": "class ProviderInvocationFamily(IntEnum):\n    PRIMARY = 0\n    ADVISOR = 1\n    PLANNER = 2\n    TITLE = 3",
    "elspeth.web.composer.provider_quota:ProviderInvocationOwner": 'class ProviderInvocationOwner:\n    """Single explicit mint owner for independently ordered provider families."""\n\n    def __init__(self, *, service: SessionServiceProtocol, required_work: RequiredWorkBinding) -> None:\n        if type(required_work) is not RequiredWorkBinding or required_work.role is not RequiredWorkRole.TURN:\n            raise AuditIntegrityError(\'Provider invocation owner requires its exact turn binding\')\n        required_work.validate_context(required_work.coordinator.authority.context)\n        self._service = service\n        self._required_work = required_work\n        self._counters = dict.fromkeys(ProviderInvocationFamily, 0)\n\n    @property\n    def service(self) -> SessionServiceProtocol:\n        return self._service\n\n    @property\n    def required_work(self) -> RequiredWorkBinding:\n        return self._required_work\n\n    def mint(self, family: ProviderInvocationFamily) -> ProviderCallCustody:\n        if type(family) is not ProviderInvocationFamily:\n            raise AuditIntegrityError(\'Provider family is not an owned nominal identity\')\n        ordinal = self._counters[family]\n        self._counters[family] = ordinal + 1\n        diagonal = int(family) + ordinal\n        semantic = diagonal * (diagonal + 1) // 2 + ordinal\n        binding = RequiredWorkBinding(self.required_work.coordinator, self.required_work.transition_ordinal, semantic, RequiredWorkRole.TITLE if family is ProviderInvocationFamily.TITLE else RequiredWorkRole.TURN)\n        return ProviderCallCustody(service=self.service, context=binding.coordinator.authority.context, required_work=binding)',
    "elspeth.web.composer.provider_quota:ProviderCallCustody": "class ProviderCallCustody:\n    \"\"\"Explicit owned admission, physical dispatch and audit settlement custody.\"\"\"\n    __slots__ = ('__weakref__', '_admitting', '_context', '_required_work', '_sdk_entered', '_service', '_settlements', '_undispatched_recurrence', 'attempt', 'call')\n\n    def __init__(self, *, service: SessionServiceProtocol, context: SessionOperationContext, required_work: RequiredWorkBinding) -> None:\n        if type(required_work) is not RequiredWorkBinding:\n            raise AuditIntegrityError('Provider custody requires a nominal explicit binding')\n        required_work.validate_context(context)\n        if context.operation_kind is not SessionOperationKind.COMPOSE:\n            raise AuditIntegrityError('Provider custody requires exact COMPOSE authority')\n        self._service = service\n        self._context = context\n        self._required_work = required_work\n        self.attempt: ProviderAttempt | None = None\n        self.call: ComposerLLMCall | None = None\n        self._sdk_entered = False\n        self._admitting = False\n        self._undispatched_recurrence = 0\n        self._settlements = 0\n\n    @property\n    def service(self) -> SessionServiceProtocol:\n        return self._service\n\n    @property\n    def context(self) -> SessionOperationContext:\n        return self._context\n\n    @property\n    def required_work(self) -> RequiredWorkBinding:\n        return self._required_work\n\n    async def _join[R](self, operation: Coroutine[Any, Any, R]) -> tuple[R, tuple[asyncio.CancelledError, ...]]:\n        task = asyncio.create_task(operation)\n        cancellations: list[asyncio.CancelledError] = []\n        while not task.done():\n            try:\n                await asyncio.shield(task)\n            except asyncio.CancelledError as cancellation:\n                if all((cancellation is not original for original in cancellations)):\n                    cancellations.append(cancellation)\n            except BaseException:\n                break\n        if task.cancelled():\n            try:\n                task.result()\n            except asyncio.CancelledError as child_cancellation:\n                integrity = AuditIntegrityError('Required provider child lacks an actual delivered outcome')\n                integrity.__cause__ = child_cancellation\n                if cancellations:\n                    raise BaseExceptionGroup('Provider self-cancelled child and original cancellations', [integrity, child_cancellation, *cancellations]) from None\n                raise integrity from child_cancellation\n        try:\n            return (task.result(), tuple(cancellations))\n        except BaseException as failure:\n            retained = failure\n            if isinstance(failure, Exception) and (not isinstance(failure, (*contract_errors.TIER_1_ERRORS, SQLAlchemyError, SessionOperationFenceLost, ChargeableAdmissionRefused, BaseExceptionGroup))):\n                retained = ComposerOwnedSettlementFailure()\n                retained.__cause__ = failure\n            if cancellations:\n                raise BaseExceptionGroup('Provider required child and cancellation outcomes', [retained, *cancellations]) from None\n            if retained is not failure:\n                raise retained from failure\n            raise\n\n    def raise_deferred_cancellations(self, cancellations: tuple[asyncio.CancelledError, ...]) -> None:\n        if len(cancellations) == 1:\n            raise cancellations[0]\n        if cancellations:\n            raise BaseExceptionGroup('Provider original cancellations', list(cancellations))\n\n    async def join_title_update[R](self, operation: Coroutine[Any, Any, R]) -> tuple[R, tuple[asyncio.CancelledError, ...]]:\n        if self.required_work.role is not RequiredWorkRole.TITLE or self.attempt is not None:\n            raise AuditIntegrityError('Title publication cannot precede provider accounting completion')\n        return await self._join(operation)\n\n    async def _cancel_undispatched(self, *, model: str) -> tuple[asyncio.CancelledError, ...]:\n        if self.attempt is None or self._sdk_entered:\n            raise AuditIntegrityError('Undispatched disposition lacks exact no-SDK attempt evidence')\n        binding = self.required_work\n        ticket = binding.coordinator.reserve(RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL, transition_ordinal=binding.transition_ordinal, semantic_ordinal=binding.semantic_ordinal, recurrence_ordinal=self._undispatched_recurrence)\n        self._undispatched_recurrence += 1\n        _, cancellations = await self._join(self.service.cancel_undispatched_provider_attempt(session_operation_context=self.context, attempt_id=self.attempt.attempt_id, requested_model=model, required_work=ticket))\n        self.attempt = None\n        self.call = None\n        return cancellations\n\n    async def cancel_before_sdk(self, *, model: str) -> tuple[asyncio.CancelledError, ...]:\n        \"\"\"Dispose an actually admitted attempt whose physical SDK never began.\"\"\"\n        return await self._cancel_undispatched(model=model)\n\n    async def admit(self, *, model: str) -> None:\n        if type(model) is not str or not model or self._admitting:\n            raise AuditIntegrityError('Provider admission lacks a single owned model invocation')\n        self._admitting = True\n        try:\n            await self.settle()\n            title = self.required_work.role is RequiredWorkRole.TITLE\n            sql, projection = self.required_work.reserve_pair(RequiredWorkSource.TITLE_PROVIDER_ADMISSION_SQL if title else RequiredWorkSource.PROVIDER_ADMISSION_SQL, RequiredWorkSource.TITLE_PROVIDER_ADMISSION_PROJECTION if title else RequiredWorkSource.PROVIDER_ADMISSION_PROJECTION)\n            try:\n                attempt, cancellations = await self._join(self.service.begin_provider_attempt(session_operation_context=self.context, source='auto_title' if title else 'composer', required_work=sql))\n            except BaseException:\n                if sql.complete:\n                    projection.complete_without_submission()\n                raise\n            projection.begin_projection()\n            try:\n                if type(attempt) is not ProviderAttempt:\n                    raise AuditIntegrityError('Provider admission returned a foreign attempt')\n                self.attempt = attempt\n                self.call = None\n                self._sdk_entered = False\n            except BaseException as failure:\n                projection.complete_owned(failure)\n                raise\n            projection.complete_owned()\n            if cancellations:\n                additional = await self._cancel_undispatched(model=model)\n                self.raise_deferred_cancellations((*cancellations, *additional))\n        finally:\n            self._admitting = False\n\n    def mark_sdk_entered(self) -> None:\n        if self.attempt is None or self._sdk_entered or self.call is not None:\n            raise AuditIntegrityError('Physical provider dispatch lacks exact admitted custody')\n        self._sdk_entered = True\n\n    def needs_terminal_audit(self) -> bool:\n        return self.attempt is not None and self._sdk_entered and (self.call is None)\n\n    def bind_call(self, call: ComposerLLMCall) -> ComposerLLMCall:\n        if type(call) is not ComposerLLMCall:\n            raise AuditIntegrityError('Provider audit is not an owned complete call')\n        if self.attempt is None or not self._sdk_entered:\n            raise AuditIntegrityError('Provider audit lacks actual dispatched admission')\n        bound = replace(call, call_id=self.attempt.attempt_id, started_at=self.attempt.started_at)\n        self.call = bound\n        return bound\n\n    def retain_audit(self, call: ComposerLLMCall) -> bool:\n        if type(call) is not ComposerLLMCall:\n            raise AuditIntegrityError('Provider retained audit is not nominal')\n        if self.attempt is None:\n            return False\n        if not self._sdk_entered or call.call_id != self.attempt.attempt_id or call.started_at != self.attempt.started_at:\n            raise AuditIntegrityError('Provider audit replaced actual admitted identity')\n        self.call = call\n        return True\n\n    async def settle(self) -> None:\n        if self.attempt is None:\n            return\n        if not self._sdk_entered or self.call is None:\n            raise AuditIntegrityError('Required provider attempt lacks complete terminal audit')\n        binding = self.required_work\n        projection: RequiredWorkTicket | None = None\n        if binding.role is RequiredWorkRole.TITLE:\n            sql = binding.coordinator.reserve(RequiredWorkSource.TITLE_PROVIDER_SETTLEMENT_SQL, transition_ordinal=binding.transition_ordinal, semantic_ordinal=binding.semantic_ordinal, recurrence_ordinal=self._settlements)\n            self._settlements += 1\n        else:\n            sql, projection = binding.reserve_pair(RequiredWorkSource.PROVIDER_SETTLEMENT_SQL, RequiredWorkSource.PROVIDER_SETTLEMENT_PROJECTION)\n        try:\n            _, cancellations = await self._join(self.service.finish_provider_attempt(session_operation_context=self.context, call=self.call, required_work=sql))\n        except BaseException:\n            if projection is not None and sql.complete:\n                projection.complete_without_submission()\n            raise\n        if projection is not None:\n            projection.begin_projection()\n            projection.complete_owned()\n        self.attempt = None\n        self.call = None\n        self._sdk_entered = False\n        self.raise_deferred_cancellations(cancellations)",
    "elspeth.web.sessions.composer_turn:_run_composer_turn": "async def _run_composer_turn(services: ComposerAppServices, turn: ComposerTurnInput, *, lease: SessionOperationLease, running: ComposerOperationRunning, request_lifecycle: ComposerRequestLifecycle, observation: ComposerTurnObservation, budget_anchor: ComposerBudgetAnchor) -> ComposerOperationRecord:\n    \"\"\"Run one freeform send or recompose turn to its settled success; see the module docstring.\"\"\"\n    deferred_cancellation: asyncio.CancelledError | None = None\n    continuation_cancellations: list[asyncio.CancelledError] = []\n    auto_title_cancellations: list[asyncio.CancelledError] = []\n    if running.claim.session_id != turn.session_id or running.claim.operation_id != turn.operation_id:\n        raise AuditIntegrityError('composer turn input does not match its running operation')\n    if lease.context != running.session_operation_context:\n        raise AuditIntegrityError(\"composer turn lease is not the running operation's session operation context\")\n    if observation.required_work is None:\n        required_work = lease.required_work\n        observation.required_work = RequiredWorkCoordinator(RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, lease.context, running.claim.operation_id, running.claim.attempt)) if required_work is None else required_work\n    required_work = observation.required_work\n    if type(required_work) is not RequiredWorkCoordinator:\n        raise AuditIntegrityError('Composer turn requires an owned required-work coordinator')\n    lease.bind_required_work(required_work)\n    if turn.budget_seconds != budget_anchor.remaining_at_running_seconds:\n        raise ValueError(\"composer turn budget_seconds is not its budget anchor's remaining_at_running_seconds\")\n    if observation.compose_result is not None or observation.pending_exception_llm_calls or observation.audit_cohort_durable or (observation.compose_base_state_id is not None):\n        raise ValueError('composer turn observation must be fresh for each turn')\n    if request_lifecycle.durable_completed:\n        raise ValueError('composer turn request lifecycle is already durably completed')\n    labels = _SEND_LABELS if turn.kind == 'compose_message' else _RECOMPOSE_LABELS\n    request = turn.request\n    service = services.session_service\n    settings = services.settings\n    provider_binding = RequiredWorkBinding(required_work, 0, 0, RequiredWorkRole.TURN, running)\n    provider_owner = ProviderInvocationOwner(service=service, required_work=provider_binding)\n    try:\n        session = await service.get_session(turn.session_id)\n    except SessionNotFoundError:\n        raise HTTPException(status_code=404, detail='Session not found') from None\n    state_record = await service.get_current_state(session.id)\n    compose_base_state_id = state_record.id if state_record is not None else None\n    if compose_base_state_id != request.state_id:\n        raise AuditIntegrityError(f'composer operation {turn.operation_id}: the head under its adopted COMPOSE lease is not the bound base the start composite verified')\n    state = _initial_composition_state() if state_record is None else _state_from_record(state_record)\n    prior_completion_gates_facts = parse_completion_gates(state_record.composer_meta) if state_record is not None else None\n    plugin_snapshot = services.plugin_snapshot_for_user_id(turn.actor_user_id)\n    profile_registry = services.operator_profile_registry\n    if isinstance(request, SendMessageRequest):\n        message_text = request.content\n        ingress_sql, ingress_projection = observation.required_work.reserve_pair(RequiredWorkSource.INGRESS_SQL, RequiredWorkSource.INGRESS_PROJECTION, transition_ordinal=0, semantic_ordinal=0)\n        ingress = lease.create_task(_capture_freeform_child(service.add_message_with_transcript(session.id, 'user', request.content, operation_id=UUID(turn.operation_id), requested_state_id=request.state_id, composition_state_id=compose_base_state_id, writer_principal='route_user_message', session_operation_context=lease.context, running=running, required_work=ingress_sql)), name='send-message-ingress')\n        try:\n            ingress_outcome, ingress_cancellation = await _join_freeform_owned_task(ingress)\n            ingress_result = _freeform_child_result(ingress_outcome)\n        except BaseException:\n            ingress_projection.complete_owned()\n            raise\n        try:\n            if not isinstance(ingress_result, MessageIngressFresh):\n                raise AuditIntegrityError(f'composer operation {turn.operation_id}: message ingress was not fresh for a running job')\n            turn_user_message_id = ingress_result.message.id\n            records = list(ingress_result.transcript)\n        except BaseException as exc:\n            ingress_projection.complete_owned(exc)\n            raise\n        ingress_projection.complete_owned()\n        if ingress_cancellation is not None:\n            raise ingress_cancellation\n    else:\n        records = await service.get_messages(session.id, limit=None)\n        recompose_conversation = _composer_conversation_messages(records)\n        latest_user = next((row for row in reversed(recompose_conversation) if row.role == 'user'), None)\n        if latest_user is None or latest_user.id != request.expected_user_message_id:\n            raise AuditIntegrityError(f'composer operation {turn.operation_id}: the recompose transcript changed under its adopted COMPOSE lease after the start composite verified it')\n        latest_user_index = recompose_conversation.index(latest_user)\n        if any((row.role == 'assistant' and (not row.tool_calls) for row in recompose_conversation[latest_user_index + 1:])):\n            raise AuditIntegrityError('Recompose saved response appeared under its exact COMPOSE lease')\n        proposals = await service.list_composition_proposals(session.id)\n        if any((p.user_message_id == latest_user.id and p.pipeline_metadata is not None and (p.status in ('pending', 'committed')) for p in proposals)):\n            raise AuditIntegrityError('Recompose saved proposal appeared under its exact COMPOSE lease')\n        message_text = latest_user.content\n        turn_user_message_id = latest_user.id\n    chat_ingress = compartment_ingress_record(message_text, own_compartment_id=settings.compartment_id)\n    chat_ingress_inputs = _chat_ingress_inputs(records, own_compartment_id=settings.compartment_id)\n    progress_sink = await _advisory_progress_sink(services, request_lifecycle=request_lifecycle, session_id=session.id, operation_id=turn.operation_id, running=running, request_id=str(turn_user_message_id), user_id=turn.actor_user_id)\n    await _publish_progress(progress_sink, event=ComposerProgressEvent(phase='starting', headline=labels.starting_headline, evidence=(labels.starting_evidence,), likely_next='ELSPETH will prepare the composer prompt with the current pipeline.'))\n    _COMPOSER_REQUESTS_INFLIGHT.add(1, {'endpoint': labels.endpoint})\n    terminal_status: _ComposerRequestTerminalStatus = 'failed'\n    auto_title_task: asyncio.Task[None] | None = None\n    try:\n        if isinstance(request, SendMessageRequest):\n            send_conversation = _composer_conversation_messages(records)\n            if not send_conversation or send_conversation[-1].id != turn_user_message_id:\n                raise AuditIntegrityError(f'Tier 1 audit anomaly: send_message transcript snapshot for session {session.id} does not end at inserted user message {turn_user_message_id}. Refusing to compose against interleaved session history.')\n        has_partial_turn = isinstance(request, RecomposeRequest) and any((row.role == 'assistant' for row in recompose_conversation[latest_user_index + 1:]))\n        chat_messages = _composer_chat_history(records if has_partial_turn else [record for record in records if record.id != turn_user_message_id])\n        if isinstance(request, SendMessageRequest) and len(records) == 1 and is_default_session_title(session.title):\n            title_custody = provider_owner.mint(ProviderInvocationFamily.TITLE)\n            title_producer = required_work.reserve(RequiredWorkSource.TITLE_CONTINUATION_PRODUCER, transition_ordinal=title_custody.required_work.transition_ordinal, semantic_ordinal=title_custody.required_work.semantic_ordinal)\n\n            async def run_owned_title() -> None:\n                try:\n                    await maybe_auto_title_session(service=service, session_id=session.id, user_message=request.content, model=settings.composer_model, temperature=settings.composer_temperature, seed=settings.composer_seed, session_operation_context=lease.context, api_base=settings.composer_endpoint_base_url, api_key=settings.composer_endpoint_api_key.get_secret_value() if settings.composer_endpoint_api_key is not None else None, provider_custody=title_custody)\n                except BaseException as failure:\n                    title_producer.complete_owned(failure)\n                    raise\n                title_producer.complete_owned()\n            try:\n                auto_title_task = lease.create_task(run_owned_title())\n            except BaseException as failure:\n                title_producer.complete_owned(failure)\n                raise\n        composer = services.composer_service\n        from openai import OpenAIError\n\n        async def settle_post_provider(result: ComposerResult) -> ComposerOperationRecord:\n            _post_compose_updates: ComposerPostComposeUpdates = {'repair_turns_used': result.repair_turns_used, 'ingress': chat_ingress, 'chat_ingress_inputs': chat_ingress_inputs}\n            _post_compose_meta = merge_composer_meta_updates(state_record.composer_meta if state_record is not None else None, _post_compose_updates)\n            state_response: CompositionStateResponse | None = None\n            live_validation: ValidationSummary | None = None\n            post_compose_state_id: UUID | None = compose_base_state_id\n            assistant_record: ChatMessageRecord | None = None\n            route_settlement: PipelineRouteSettlement | None = None\n            if result.pipeline_commit_intent is not None:\n                commit_timeout_seconds = budget_anchor.remaining_seconds(monotonic_now=time.monotonic())\n                if commit_timeout_seconds <= 0:\n                    raise HTTPException(status_code=504, detail=_SETTLEMENT_TIMEOUT_DETAIL)\n                settlement_outcome = await settle_auto_commit_intent(services=services, user_id=turn.actor_user_id, service=service, session_id=session.id, intent=result.pipeline_commit_intent, composer_meta=_post_compose_meta, telemetry_source=labels.telemetry_source, session_operation_context=lease.context, commit_timeout_seconds=commit_timeout_seconds, running=running, required_work=observation.required_work, required_binding=provider_binding)\n                if type(settlement_outcome) is PipelineRouteSettlement:\n                    route_settlement = settlement_outcome\n                else:\n                    result = replace(result, message=PIPELINE_STAGED_REVIEW_MESSAGE, pipeline_commit_intent=None)\n            turn_end = composer_turn_end_assistant_row(result)\n            if route_settlement is not None:\n                live_validation = route_settlement.validation\n                state_response = _state_response(route_settlement.settlement.state, live_validation=route_settlement.validation)\n                post_compose_state_id = route_settlement.settlement.state.id\n                assistant_record = route_settlement.settlement.transition_message\n            elif result.state.version != state.version or completion_gate_decision_changes(prior_completion_gates_facts, result.advisor_gate_decision, result.state):\n                await _publish_progress(progress_sink, event=ComposerProgressEvent(phase='validating', headline='The composer is validating the pipeline and its review status.', evidence=('The pipeline state and review outcome are being checked before persistence.',), likely_next='ELSPETH will save the validated pipeline snapshot.'))\n                try:\n                    state_data, validation = await _state_data_from_composer_state(result.state, settings=settings, secret_service=services.scoped_secret_resolver, user_id=turn.actor_user_id, session_id=session.id, plugin_snapshot=plugin_snapshot, profile_registry=profile_registry, catalog=services.catalog_service, runtime_preflight=result.runtime_preflight, preflight_exception_policy='raise', initial_version=state.version, telemetry_source=labels.telemetry_source, composer_meta=_post_compose_meta, advisor_gate_decision=result.advisor_gate_decision)\n                except ComposerRuntimePreflightError as rpf_exc:\n                    rpf_exc = ComposerRuntimePreflightError(original_exc=rpf_exc.original_exc, partial_state=rpf_exc.partial_state, tool_invocations=result.tool_invocations, llm_calls=result.llm_calls)\n                    observation.pending_exception_llm_calls = _llm_calls_from_exception(rpf_exc)\n                    await _publish_progress(progress_sink, event=ComposerProgressEvent(phase='failed', headline='The composer could not safely validate the pipeline update.', evidence=('Runtime preflight failed during state persistence.',), likely_next='Review the visible error message, then retry after the issue is resolved.', reason='runtime_preflight_failed'))\n                    response_body = await _required_audit(observation, _handle_runtime_preflight_failure(rpf_exc, service, session.id, turn.actor_user_id, labels.handler_log_prefix, compose_base_state_id, settings=settings, secret_service=services.scoped_secret_resolver, plugin_snapshot=plugin_snapshot, profile_registry=profile_registry, catalog=services.catalog_service, session_operation_context=lease.context, ingress=chat_ingress, chat_ingress_inputs=chat_ingress_inputs, required_audit=True, required_work=required_work))\n                    observation.audit_cohort_durable = True\n                    raise HTTPException(status_code=500, detail=response_body) from rpf_exc.original_exc\n                await _publish_progress(progress_sink, event=ComposerProgressEvent(phase='saving', headline='ELSPETH is saving the pipeline and review status.', evidence=('A new composition state version is being stored for this session.',), likely_next='The assistant response will appear after the save completes.'))\n                checkpoint_sql, checkpoint_projection = required_work.reserve_pair(RequiredWorkSource.COMPOSE_CHECKPOINT_SQL, RequiredWorkSource.COMPOSE_CHECKPOINT_PROJECTION, transition_ordinal=0, semantic_ordinal=0)\n                try:\n                    new_state_record = await service.save_composition_state(session.id, state_data, provenance='post_compose', session_operation_context=lease.context, required_work=checkpoint_sql)\n                except BaseException:\n                    checkpoint_projection.complete_owned()\n                    raise\n                try:\n                    state_response = _state_response(new_state_record, live_validation=validation)\n                    live_validation = validation\n                    post_compose_state_id = new_state_record.id\n                except BaseException as exc:\n                    checkpoint_projection.complete_owned(exc)\n                    raise\n                checkpoint_projection.complete_owned()\n            assistant_write: ComposerOperationAssistantWrite | None\n            parent_assistant_id: UUID\n            if assistant_record is None:\n                parent_assistant_id = uuid4()\n                assistant_write = ComposerOperationAssistantWrite(message_id=parent_assistant_id, content=turn_end.content, raw_content=turn_end.raw_content, composition_state_id=post_compose_state_id)\n            else:\n                assistant_write = None\n                parent_assistant_id = assistant_record.id\n            audit_cohort, _pipeline_bindings = _turn_audit_cohort_drafts(result.tool_invocations if not result.persisted_tool_call_turn else (), result.llm_calls, tool_composition_state_id=post_compose_state_id, llm_composition_state_id=compose_base_state_id, parent_assistant_id=parent_assistant_id)\n            record = await _join_shielded_task_after_cancellation(asyncio.create_task(service.complete_composer_async_operation(running, assistant=assistant_write, assistant_record=assistant_record, audit_cohort=audit_cohort, audit_composition_state_id=None, required_work=observation.required_work, build_response=partial(_message_with_state_response, state_response=state_response, live_validation=live_validation)), name='composer-operation-terminal-composite'))\n            observation.audit_cohort_durable = True\n            await _publish_progress(progress_sink, event=ComposerProgressEvent(phase='complete', headline='The composer has updated the pipeline.' if result.state.version != state.version else 'The composer response is ready.', evidence=('The assistant response has been saved for this session.',), likely_next='Review the response and current pipeline.', reason='composer_complete'))\n            return record\n        post_provider_error: BaseException | None = None\n        settled_record: ComposerOperationRecord | None = None\n        compose_budget_seconds = budget_anchor.remaining_seconds(monotonic_now=time.monotonic())\n        observation.compose_base_state_id = compose_base_state_id\n        if compose_budget_seconds <= 0:\n            terminal_status = 'timed_out'\n            await _publish_progress(progress_sink, event=convergence_progress_event(budget_exhausted='timeout'))\n            raise ComposerTurnDeadlineExpired(session_id=turn.session_id, operation_id=turn.operation_id, remaining_seconds=compose_budget_seconds, budget_seconds_at_running=budget_anchor.remaining_at_running_seconds)\n        try:\n            result = await composer.compose(message_text, chat_messages, state, session_id=str(turn.session_id), current_state_id=str(compose_base_state_id) if compose_base_state_id is not None else None, user_id=turn.actor_user_id, progress=progress_sink, session_operation_context=lease.context, user_message_id=str(turn_user_message_id), completion_gates=prior_completion_gates_facts, budget_seconds=compose_budget_seconds, required_work=provider_binding, provider_owner=provider_owner)\n            observation.compose_result = result\n            if auto_title_task is not None:\n                try:\n                    await _join_auto_title(auto_title_task, session_id=turn.session_id, operation_id=turn.operation_id, cancellation_observations=auto_title_cancellations)\n                finally:\n                    auto_title_task = None\n            continuation = lease.create_task(_capture_freeform_child(settle_post_provider(result)), name=labels.settlement_task_name)\n            try:\n                outcome, deferred_cancellation = await _join_freeform_owned_task(continuation, cancellation_observations=continuation_cancellations)\n                receipt = _freeform_child_result(outcome)\n            except (AuditIntegrityError, SQLAlchemyError, HTTPException, InvariantError, ComposerConvergenceError, ComposerRuntimePreflightError, ComposerPluginCrashError, ComposerServiceError, PipelinePlannerError, ChargeableAdmissionRefused, ComposerAdmissionRefused, OpenAIError, _BadRequestLLMError, ComposerOperationCancelledDuringTurn, ComposerOperationFenceLost, SessionOperationFenceLost, AsyncWorkerAdmissionTimeoutError, asyncio.CancelledError) as exc:\n                post_provider_error = exc\n            except Exception as exc:\n                post_provider_error = ComposerOwnedSettlementFailure()\n                post_provider_error.__cause__ = exc\n            else:\n                settled_record = receipt\n                request_lifecycle.durable_completed = True\n                terminal_status = 'completed'\n        except asyncio.CancelledError as turn_cancel:\n            if post_provider_error is not None:\n                raise post_provider_error from turn_cancel\n            raise\n        except ComposerConvergenceError as exc:\n            observation.pending_exception_llm_calls = _llm_calls_from_exception(exc)\n            terminal_status = 'timed_out' if exc.budget_exhausted == 'timeout' else 'failed'\n            await _publish_progress(progress_sink, event=convergence_progress_event(budget_exhausted=exc.budget_exhausted))\n            observation.pending_exception_tool_invocations = exc.tool_invocations if exc.failed_turn is None else ()\n            response_body = await _required_audit(observation, _handle_convergence_error(exc, service, session.id, turn.actor_user_id, labels.convergence_log_prefix, compose_base_state_id, settings=settings, secret_service=services.scoped_secret_resolver, plugin_snapshot=plugin_snapshot, profile_registry=profile_registry, catalog=services.catalog_service, session_operation_context=lease.context, ingress=chat_ingress, chat_ingress_inputs=chat_ingress_inputs, budget_seconds=compose_budget_seconds, required_audit=True, required_work=required_work))\n            observation.audit_cohort_durable = True\n            raise HTTPException(status_code=422, detail=response_body) from exc\n        except OpenAIError as exc:\n            observation.pending_exception_llm_calls = _llm_calls_from_exception(exc)\n            provider_failure = await _required_audit(observation, _handle_composer_provider_failure(exc, route=labels.provider_route, service=service, session_id=session.id, composition_state_id=compose_base_state_id, progress_sink=progress_sink, session_operation_context=lease.context, expose_provider_error=settings.composer_expose_provider_errors, required_audit=True, required_work=observation.required_work))\n            observation.audit_cohort_durable = True\n            raise provider_failure from exc\n        except _BadRequestLLMError as exc:\n            llm_calls = _llm_calls_from_exception(exc)\n            observation.pending_exception_llm_calls = llm_calls\n            slog.error(labels.llm_bad_request_event, session_id=str(turn.session_id), exc_class=type(exc).__name__)\n            await _publish_progress(progress_sink, event=ComposerProgressEvent(phase='failed', headline=f'The composer model rejected this {labels.noun}.', evidence=('The model provider rejected the composer request as invalid.',), likely_next='Check the composer provider configuration and request options before retrying.', reason='provider_unavailable'))\n            if llm_calls:\n                await _required_audit(observation, _persist_llm_calls(service, session.id, llm_calls, compose_base_state_id, plugin_crash_pending=True, session_operation_context=lease.context, required_audit=True, required_work=observation.required_work))\n                observation.audit_cohort_durable = True\n            raise HTTPException(status_code=502, detail=_litellm_error_detail('llm_unavailable', exc, expose_provider_error=settings.composer_expose_provider_errors)) from exc\n        except ComposerPluginCrashError as crash:\n            observation.pending_exception_llm_calls = _llm_calls_from_exception(crash)\n            observation.pending_exception_tool_invocations = crash.tool_invocations if crash.failed_turn is None else ()\n            response_body = await _required_audit(observation, _handle_plugin_crash(crash, service, session.id, turn.actor_user_id, labels.handler_log_prefix, compose_base_state_id, settings=settings, secret_service=services.scoped_secret_resolver, plugin_snapshot=plugin_snapshot, profile_registry=profile_registry, catalog=services.catalog_service, session_operation_context=lease.context, ingress=chat_ingress, chat_ingress_inputs=chat_ingress_inputs, required_audit=True, required_work=required_work))\n            observation.audit_cohort_durable = True\n            await _publish_progress(progress_sink, event=ComposerProgressEvent(phase='failed', headline=f'The composer could not safely finish this {labels.noun}.', evidence=('A pipeline tool failed on the server side.',), likely_next='Review the visible error message, then retry after the issue is resolved.', reason='plugin_crash'))\n            raise HTTPException(status_code=500, detail=response_body) from crash.original_exc\n        except ComposerRuntimePreflightError as rpf_exc:\n            observation.pending_exception_llm_calls = _llm_calls_from_exception(rpf_exc)\n            _record_composer_runtime_preflight_telemetry('exception', source='cached_preflight', exception_class=rpf_exc.exc_class)\n            await _publish_progress(progress_sink, event=ComposerProgressEvent(phase='failed', headline=f'The composer could not safely finish this {labels.noun}.', evidence=('Runtime preflight failed before the compose loop returned.',), likely_next='Review the visible error message, then retry after the issue is resolved.', reason='runtime_preflight_failed'))\n            observation.pending_exception_tool_invocations = rpf_exc.tool_invocations\n            response_body = await _required_audit(observation, _handle_runtime_preflight_failure(rpf_exc, service, session.id, turn.actor_user_id, labels.handler_log_prefix, compose_base_state_id, settings=settings, secret_service=services.scoped_secret_resolver, plugin_snapshot=plugin_snapshot, profile_registry=profile_registry, catalog=services.catalog_service, session_operation_context=lease.context, ingress=chat_ingress, chat_ingress_inputs=chat_ingress_inputs, required_audit=True, required_work=required_work))\n            observation.audit_cohort_durable = True\n            raise HTTPException(status_code=500, detail=response_body) from rpf_exc.original_exc\n        except PipelinePlannerError as exc:\n            observation.pending_exception_llm_calls = _llm_calls_from_exception(exc)\n            await _publish_progress(progress_sink, event=ComposerProgressEvent(phase='failed', headline=f'The composer could not build a pipeline for this {labels.noun}.', evidence=('Cost accounting could not admit the model response.' if exc.code == 'COST_UNAVAILABLE' else 'The composer model did not return a usable pipeline plan.',), likely_next='Ask an administrator to configure or correct model pricing before trying again.' if exc.code == 'COST_UNAVAILABLE' else 'Retry the request; if it keeps failing, simplify it or check the composer provider.', reason=freeform_planner_progress_reason(exc.code)))\n            status_code, planner_response_body = await _required_audit(observation, _handle_planner_failure(exc, service, session.id, compose_base_state_id, session_operation_context=lease.context, required_audit=True, required_work=observation.required_work))\n            raise HTTPException(status_code=status_code, detail=planner_response_body) from exc\n        except ChargeableAdmissionRefused as exc:\n            observation.pending_exception_llm_calls = _llm_calls_from_exception(exc)\n            chargeable_refusal = await _required_audit(observation, _handle_composer_chargeable_refusal(exc, service=service, session_id=session.id, composition_state_id=compose_base_state_id, progress_sink=progress_sink, session_operation_context=lease.context, required_audit=True, required_work=observation.required_work))\n            observation.audit_cohort_durable = True\n            raise chargeable_refusal from exc\n        except ComposerAdmissionRefused as exc:\n            observation.pending_exception_llm_calls = _llm_calls_from_exception(exc)\n            await _publish_progress(progress_sink, event=ComposerProgressEvent(phase='failed', headline='This request was refused by the admission policy.', evidence=(str(exc),), likely_next='Ask an administrator to review your access and quota configuration.', reason='admission_refused'))\n            if isinstance(exc, CredentialMaterialRefused):\n                raise HTTPException(status_code=422, detail=exc.to_payload()) from exc\n            raise HTTPException(status_code=403, detail={'error_type': 'composer_admission_refused', 'failure_code': 'admission_refused', 'detail': str(exc)}) from exc\n        except ComposerServiceError as exc:\n            llm_calls = _llm_calls_from_exception(exc)\n            observation.pending_exception_llm_calls = llm_calls\n            await _publish_progress(progress_sink, event=ComposerProgressEvent(phase='failed', headline=f'The composer could not finish this {labels.noun}.', evidence=('Prompt preparation or composer service setup failed.',), likely_next='Retry once the composer service is available.', reason='service_setup_failed'))\n            if llm_calls:\n                await _required_audit(observation, _persist_llm_calls(service, session.id, llm_calls, compose_base_state_id, plugin_crash_pending=True, session_operation_context=lease.context, required_audit=True, required_work=observation.required_work))\n                observation.audit_cohort_durable = True\n            raise HTTPException(status_code=502, detail={'error_type': 'composer_error', 'detail': str(exc)}) from exc\n        finally:\n            current_exc = sys.exception()\n            drained_calls = () if current_exc is None or isinstance(current_exc, asyncio.CancelledError) else _llm_calls_from_exception(current_exc)\n            if drained_calls:\n                observation.pending_exception_llm_calls = drained_calls\n                await _required_audit(observation, _persist_llm_calls(service, session.id, drained_calls, compose_base_state_id, plugin_crash_pending=True, session_operation_context=lease.context, required_audit=True, required_work=observation.required_work))\n                observation.audit_cohort_durable = True\n        if post_provider_error is not None:\n            raise post_provider_error\n        if settled_record is None:\n            raise InvariantError('Provider returned without a freeform continuation receipt')\n        if deferred_cancellation is not None:\n            raise deferred_cancellation\n        if observation.deferred_cancellation is not None:\n            raise observation.deferred_cancellation\n        return settled_record\n    except ComposerRequiredAuditPersistenceError as exc:\n        _COMPOSER_TIER1_VIOLATION_COUNTER.add(1, {'helper': exc.helper})\n        raise\n    except InvariantError as exc:\n        slog.error('composer.invariant_violated', session_id=str(turn.session_id), user_id=turn.actor_user_id, exc_class=type(exc).__name__, site=labels.site, frames=_safe_frame_strings(exc))\n        original_outcomes: list[BaseException] = [exc]\n        for cancelled in (*continuation_cancellations, deferred_cancellation, observation.deferred_cancellation):\n            if cancelled is not None and all((cancelled is not original for original in original_outcomes)):\n                original_outcomes.append(cancelled)\n        cause = exc if len(original_outcomes) == 1 else BaseExceptionGroup('Invariant failure retained original deferred cancellation', original_outcomes)\n        raise HTTPException(status_code=500, detail={'error_type': 'server_invariant_violated', 'detail': 'Server invariant violated. See application audit log for diagnostic detail.'}) from cause\n    except asyncio.CancelledError as exc:\n        if request_lifecycle.durable_completed:\n            terminal_status = 'completed'\n            if len(auto_title_cancellations) > 1:\n                raise BaseExceptionGroup('Auto-title join retained caller cancellations', auto_title_cancellations) from exc\n            raise\n        llm_calls = _llm_calls_from_exception(exc)\n        observation.pending_exception_llm_calls = llm_calls\n        try:\n            if llm_calls:\n                await _join_shielded_task_after_cancellation(asyncio.create_task(_persist_llm_calls(service, session.id, llm_calls, compose_base_state_id, plugin_crash_pending=True, session_operation_context=lease.context, required_audit=True, required_work=observation.required_work), name=labels.cancelled_llm_task_name), primary_cancellation=exc)\n                observation.audit_cohort_durable = True\n            user_stop = _cancel_is_user_stop(exc)\n            await _join_shielded_task_after_cancellation(asyncio.create_task(_publish_progress(progress_sink, event=client_cancelled_progress_event() if user_stop else _composer_heartbeat_failed_progress_event()), name=labels.cancelled_progress_task_name), primary_cancellation=exc)\n        except BaseException as cleanup_failure:\n            if len(auto_title_cancellations) > 1:\n                raise BaseExceptionGroup('Composer cancellation cleanup retained auto-title caller cancellations', [cleanup_failure, *auto_title_cancellations]) from cleanup_failure\n            raise\n        terminal_status = 'cancelled' if user_stop else 'failed'\n        if len(auto_title_cancellations) > 1:\n            raise BaseExceptionGroup('Auto-title join retained caller cancellations', auto_title_cancellations) from exc\n        raise\n    finally:\n        _COMPOSER_REQUESTS_INFLIGHT.add(-1, {'endpoint': labels.endpoint})\n        _record_composer_request_terminal(terminal_status, endpoint=labels.endpoint)\n        if auto_title_task is not None:\n            original = sys.exception()\n            try:\n                await _join_auto_title(auto_title_task, session_id=turn.session_id, operation_id=turn.operation_id, cancellation_observations=auto_title_cancellations)\n            except BaseException as title_failure:\n                retained: list[BaseException] = []\n                for retained_error in (original, title_failure, *auto_title_cancellations):\n                    if retained_error is not None and all((retained_error is not earlier for earlier in retained)):\n                        retained.append(retained_error)\n                if len(retained) > 1:\n                    raise BaseExceptionGroup('Composer turn retained original and auto-title join failures', retained) from title_failure\n                raise",
    "elspeth.web.composer.pipeline_planner:_new_planner_custody": "def _new_planner_custody(provider_owner: ProviderInvocationOwner) -> ProviderCallCustody:\n    if type(provider_owner) is not ProviderInvocationOwner:\n        raise AuditIntegrityError('Planner invocation needs exact explicit provider ownership')\n    return provider_owner.mint(ProviderInvocationFamily.PLANNER)",
    "elspeth.web.composer.service:ComposerServiceImpl._new_primary_custody": "def _new_primary_custody(self, provider_owner: ProviderInvocationOwner | None) -> ProviderCallCustody | None:\n    if provider_owner is None:\n        return None\n    if type(provider_owner) is not ProviderInvocationOwner or provider_owner.service is not self._require_sessions_service():\n        raise AuditIntegrityError('Primary invocation owner belongs to another exact service')\n    return provider_owner.mint(ProviderInvocationFamily.PRIMARY)",
    "elspeth.web.composer.advisor_checkpoint:AdvisorCheckpointOwner._new_advisor_custody": "def _new_advisor_custody(self, provider_owner: ProviderInvocationOwner) -> ProviderCallCustody:\n    if type(provider_owner) is not ProviderInvocationOwner or provider_owner.service is not self._require_sessions_service():\n        raise AuditIntegrityError('Advisor invocation owner belongs to another exact service')\n    return provider_owner.mint(ProviderInvocationFamily.ADVISOR)",
}


QUOTA = "elspeth.web.composer.provider_quota"
OWNER = QUOTA + ".ProviderInvocationOwner"
CUSTODY = QUOTA + ".ProviderCallCustody"
FAMILY = QUOTA + ".ProviderInvocationFamily"
BINDING = "elspeth.web.required_work.RequiredWorkBinding"


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

        def scan(node, env, *, unit=unit, postponed=postponed, parents=parents):
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
                if (unit.path, id(node)) in checked and node.name != "_run_composer_turn":
                    local["provider_owner"] = OWNER + ".__instance__"
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


def provider_mint_local_contracts(units):
    units = tuple(units)
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
        QUOTA: {"RequiredWorkBinding": BINDING, "RequiredWorkRole": "elspeth.web.required_work.RequiredWorkRole"},
        "elspeth.web.sessions.composer_turn": {"ProviderInvocationOwner": OWNER, "ProviderInvocationFamily": FAMILY},
        "elspeth.web.composer.pipeline_planner": {"ProviderInvocationOwner": OWNER, "ProviderInvocationFamily": FAMILY},
        "elspeth.web.composer.service": {"ProviderInvocationOwner": OWNER, "ProviderInvocationFamily": FAMILY},
        "elspeth.web.composer.advisor_checkpoint": {"ProviderInvocationOwner": OWNER, "ProviderInvocationFamily": FAMILY},
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
            "_new_planner_custody",
            "ComposerServiceImpl._new_primary_custody",
            "AdvisorCheckpointOwner._new_advisor_custody",
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
        and n.func.value.id == "provider_owner"
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
        ),
        "remaining_premises": (
            "UNKNOWN_COMMON_SOURCE_IMPORT_BINDING_CLOSURE",
            "UNKNOWN_COMMON_RECEIVER_MEMBER_OUTSIDE_EFFECT_CLOSURE",
            "UNKNOWN_REQUIRED_BINDING_COORDINATOR_AUTHORITY_CONTEXT_CUSTODY",
        ),
        "unclosed_callable_carriers": tuple(escapes),
    }
