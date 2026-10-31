"""Complete executable contracts for the bounded B2 Advisor source path.

These are syntax contracts, not hashes or executable substitutes. Compare ASTs
with locations/comments excluded; re-review intentional source changes.
"""

CLOSED_SOURCE_RECORDS = (
    "graph.RestoredTurnEvidence",
    "families._ToolResultFamilySource",
    "families._ToolFeedbackFamilySource",
    "families._RestoredFamilyCohort",
    "producers._FeedbackMessageProvenance",
    "producers.ContinuationToolMessagePublication",
    "producers.CurrentMessageProduction",
)

TEMPLATES: dict[str, str] = {
    "batch.run_tool_batch": r'''async def run_tool_batch(
    *,
    call_model: _CallModelOutcome,
    ctx: ToolBatchContext,
    acc: BatchAccumulator,
    llm_messages: list[dict[str, Any]],
) -> tuple[_DispatchOutcome, int]:
    """Drive the real legacy batch without continuation storage or restored owners."""
    batch = ContinuationBatchState(
        acc.state, acc.last_validation, acc.last_runtime_preflight, acc.advisor_calls_used, entry_state=acc.state
    )
    await _initialize_continuation_batch_live(call_model=call_model, ctx=ctx, acc=acc, llm_messages=llm_messages, batch=batch)
    calls = call_model.completion.tool_batch.calls
    while batch.next_tool_index < len(calls) and not batch.stop_issued_batch:
        call = ContinuationCallState(batch.next_tool_index, calls[batch.next_tool_index], batch.state, batch.state.version)
        batch.current_call = call
        batch.phase = "call_active"
        prepared = await _prepare_continuation_call_live(call_model=call_model, ctx=ctx, llm_messages=llm_messages, batch=batch, call=call)
        if prepared == "stop":
            break
        if prepared == "prepared":
            await _execute_continuation_call_live(
                call_model=call_model, ctx=ctx, acc=acc, llm_messages=llm_messages, batch=batch, call=call
            )
        batch.next_tool_index += 1
        batch.current_call = None
        batch.phase = "call_ready"
    batch.phase = "dispatch_ready"
    return _finish_continuation_batch_live(call_model=call_model, ctx=ctx, acc=acc, batch=batch)
''',
    "batch._initialize_continuation_batch_live": r'''async def _initialize_continuation_batch_live(
    *,
    call_model: _CallModelOutcome,
    ctx: ToolBatchContext,
    acc: BatchAccumulator,
    llm_messages: list[dict[str, Any]],
    batch: ContinuationBatchState,
) -> None:
    """Admit and publish the one actual assistant batch before any call."""
    if batch.phase != "uninitialized":
        raise AuditIntegrityError("tool batch initialization is not repeatable")
    recorder = ctx.recorder
    current_state_id = ctx.current_state_id
    progress = ctx.progress
    turn_sessions_service = ctx.turn_sessions_service
    turn_session_uuid = ctx.turn_session_uuid
    state = acc.state
    last_validation = acc.last_validation
    last_runtime_preflight = acc.last_runtime_preflight
    advisor_calls_used = acc.advisor_calls_used

    assistant_message_index = len(llm_messages)

    completion = call_model.completion
    assistant_message = completion.message
    raw_assistant_content = assistant_message.content
    admitted_batch = completion.tool_batch
    assistant_tool_calls = admitted_batch.calls
    mixed_abandonment_batch = False
    if len(assistant_tool_calls) > 1:
        for candidate_call in assistant_tool_calls:
            if candidate_call.function.name != "request_advisor_hint":
                continue
            try:
                candidate_arguments = bounded_json_loads(candidate_call.function.arguments, label="advisor batch admission")
            except (json.JSONDecodeError, JsonBoundaryError, TypeError, ValueError):
                continue
            if (
                type(candidate_arguments) is dict
                and "trigger" in candidate_arguments
                and candidate_arguments["trigger"] == ADVISOR_TRIGGER_CANNOT_COMPLETE
            ):
                mixed_abandonment_batch = True
                break
    if ctx.turn_evidence is not None:
        ctx.turn_evidence.invalidate_abandonment()
    # Scan the complete response metadata before progress, history, audit,
    # proposal, blob, or state effects. Internal adapters can supply an
    # already-admitted completion without passing through ProviderGateway, so
    # this common owner must cover prose, identifiers, reasoning, and mixed
    # batches all-or-nothing as well.
    require_no_credential_material_in_llm_metadata(
        content=raw_assistant_content,
        tool_calls=tuple((call.id, call.function.name) for call in assistant_tool_calls),
        reasoning_content=completion.provider_metadata.reasoning_content,
        reasoning_details=completion.provider_metadata.reasoning_details,
        thinking_blocks=completion.provider_metadata.thinking_blocks,
        model_returned=completion.provider_metadata.model_returned,
        provider_request_id=completion.provider_metadata.provider_request_id,
        finish_reason=completion.provider_metadata.finish_reason,
        provider_served=completion.provider_metadata.provider_served,
        surface="composer_tool_batch",
    )
    from elspeth.web.credential_guard import credential_state_for_tool_batch

    batch_credential_state = credential_state_for_tool_batch(state, tuple(call.function.name for call in assistant_tool_calls))
    for tool_call in assistant_tool_calls:
        require_no_credential_material_in_tool_wire(
            tool_call.function.name,
            tool_call.function.arguments,
            surface="composer_tool_wire",
            credential_state=batch_credential_state,
        )
    provider_model_version = completion.provider_metadata.model_returned or ctx.provenance.model_identifier
    if ctx.review_required:
        approval_required_mutations = sum(
            1
            for tool_call in assistant_tool_calls
            if is_mutation_tool(tool_call.function.name)
            and (
                not is_blob_store_only_mutation_tool(tool_call.function.name)
                or is_approval_required_blob_store_only_mutation_tool(tool_call.function.name)
            )
        )
        if approval_required_mutations > _MAX_PENDING_PROPOSALS_PER_TURN:
            raise _pending_proposal_cap_error(approval_required_mutations, ctx=ctx, state=state)
    await _preflight_session_tool_call_ids(
        admitted_batch,
        sessions_service=turn_sessions_service,
        session_id=turn_session_uuid,
        recorder=recorder,
    )

    await emit_progress(
        progress,
        tool_batch_progress_event(
            tuple(tool_call.function.name for tool_call in assistant_tool_calls),
        ),
    )

    # Append the assistant message (with tool_calls metadata)
    from elspeth.web.composer.continuation_message_family_builders import native_assistant_message

    assistant_wire = native_assistant_message(completion)
    _register_continuation_assistant_message(ctx, call_model, assistant_wire)
    llm_messages.append(assistant_wire)

    # Execute each tool call, tracking whether this turn has
    # budgeted discovery or mutation work. Advisor-only turns are
    # intentionally neither: they spend the advisor-specific budget,
    # not the generic discovery/composition turn budgets.
    if ctx.turn_evidence is not None:
        ctx.turn_evidence.transcript.observe_messages(llm_messages)
    turn_has_mutation = False
    turn_has_discovery = False
    all_cache_hits = True
    # Step 1 — execute tool calls in async land while accumulating
    # immutable _ToolOutcome records. Step 2 (in _persist_turn_audit)
    # performs audit writes from this list; cancellation before Step 2
    # leaves the DB unchanged for the current assistant response.
    tool_outcomes: list[_ToolOutcome] = []
    plugin_crash: ComposerPluginCrashError | None = None
    plugin_crash_cause: BaseException | None = None
    advisor_failure: Exception | None = None
    service_failure: _ServiceFailure | None = None
    advisor_compose_timeout: Literal["pre_call", "in_flight"] | None = None
    pre_state_id: str | None = current_state_id
    decoded_args_by_call_id: dict[str, dict[str, Any]] = {}
    private_authorings_by_call: dict[str, PrivateToolAuthoring] = {}
    proposals_this_turn = 0
    mutation_success_observed = False
    refused_resources: set[Resource] = set()
    promises_by_call: dict[str, frozenset[Resource]] = {}
    batch.state = state
    batch.last_validation = last_validation
    batch.last_runtime_preflight = last_runtime_preflight
    batch.advisor_calls_used = advisor_calls_used
    batch.turn_has_mutation = turn_has_mutation
    batch.turn_has_discovery = turn_has_discovery
    batch.all_cache_hits = all_cache_hits
    batch.tool_outcomes = tool_outcomes
    batch.plugin_crash = plugin_crash
    batch.plugin_crash_cause = plugin_crash_cause
    batch.advisor_failure = advisor_failure
    batch.service_failure = service_failure
    batch.advisor_compose_timeout = advisor_compose_timeout
    batch.pre_state_id = pre_state_id
    batch.decoded_args_by_call_id = decoded_args_by_call_id
    batch.private_authorings_by_call = private_authorings_by_call
    batch.proposals_this_turn = proposals_this_turn
    batch.mutation_success_observed = mutation_success_observed
    batch.refused_resources = refused_resources
    batch.promises_by_call = promises_by_call
    batch.assistant_message_index = assistant_message_index
    batch.mixed_abandonment_batch = mixed_abandonment_batch
    batch.provider_model_version = provider_model_version
    batch.phase = "call_ready" if assistant_tool_calls else "dispatch_ready"''',
    "batch._prepare_continuation_call_live": r'''async def _prepare_continuation_call_live(
    *,
    call_model: _CallModelOutcome,
    ctx: ToolBatchContext,
    llm_messages: list[dict[str, Any]],
    batch: ContinuationBatchState,
    call: ContinuationCallState,
) -> Literal["prepared", "applied", "stop"]:
    """Retain the real preparation boundary; early refusals remain outcomes."""
    recorder = ctx.recorder
    anti_anchor = ctx.anti_anchor
    actor = ctx.actor
    state = batch.state
    last_validation = batch.last_validation
    last_runtime_preflight = batch.last_runtime_preflight
    advisor_calls_used = batch.advisor_calls_used
    turn_has_mutation = batch.turn_has_mutation
    turn_has_discovery = batch.turn_has_discovery
    all_cache_hits = batch.all_cache_hits
    tool_outcomes = batch.tool_outcomes
    plugin_crash = batch.plugin_crash
    plugin_crash_cause = batch.plugin_crash_cause
    advisor_failure = batch.advisor_failure
    service_failure = batch.service_failure
    advisor_compose_timeout = batch.advisor_compose_timeout
    pre_state_id = batch.pre_state_id
    decoded_args_by_call_id = batch.decoded_args_by_call_id
    private_authorings_by_call = batch.private_authorings_by_call
    proposals_this_turn = batch.proposals_this_turn
    mutation_success_observed = batch.mutation_success_observed
    refused_resources = batch.refused_resources
    promises_by_call = batch.promises_by_call
    mixed_abandonment_batch = batch.mixed_abandonment_batch
    from elspeth.web.composer.redaction import MANIFEST

    prepared = False
    before = len(tool_outcomes)
    try:
        for tool_call in (call.tool_call,):
            if ctx.cancellation_requested.is_set():
                # The enclosing compose-loop critical section has observed a
                # cancellation. If no tool started, abort without publishing a
                # fabricated empty assistant turn. Otherwise return the completed
                # prefix so P4 can atomically publish its results before the
                # original cancellation resumes. Never start another tool after
                # the client has gone away.
                if not tool_outcomes:
                    raise _ToolBatchCancellationRequested
                break
            tool_name = tool_call.function.name
            promises_by_call[tool_call.id] = frozenset()
            pre_version = state.version
            private_arguments_canonical = private_authority_canonical = None
            private_raw_arguments = tool_call.function.arguments
            private_authorings_by_call[tool_call.id] = PrivateToolAuthoring(tool_call.id, tool_name, None, None, private_raw_arguments)
            recorder.record_private_tool_authoring(private_authorings_by_call[tool_call.id])
            unknown_audit_arguments: dict[str, Any] | None = None
            if tool_name not in MANIFEST:
                if is_discovery_tool(tool_name) or is_mutation_tool(tool_name) or is_session_aware_tool(tool_name):
                    raise AuditIntegrityError(f"Registered composer tool {tool_name!r} is missing from the redaction manifest")
                unknown_audit_arguments = cast(
                    dict[str, Any],
                    unknown_tool_arguments_redaction(telemetry=ctx.redaction_telemetry),
                )
                decoded_args_by_call_id[tool_call.id] = unknown_audit_arguments
                _replace_llm_tool_call_arguments(
                    llm_messages,
                    ctx=ctx,
                    call_model=call_model,
                    tool_call_id=tool_call.id,
                    arguments=unknown_audit_arguments,
                    dialect=ctx.tool_contract_dialect,
                    semantic=False,
                )
            # The loop always sends every tool of the dialect's W, so the sent set
            # is exactly its keys. A name outside it (hallucinated or unknown) is
            # never decoded (D17): its arguments flow on as today, with no facts.
            sent_wire_tools = _WIRE_TOOL_DEFS[ctx.tool_contract_dialect]
            tool_was_sent = tool_name in sent_wire_tools
            call_wire_facts = _CallWireFacts()
            call.wire = call_wire_facts
            if tool_was_sent and ctx.tool_contract_dialect is ToolContractDialect.OPENAI_STRICT:
                call_wire_facts.strict_sent = sent_wire_tools[tool_name].strict_capable

            def _append_tool_outcome(
                *,
                response: Any,
                error_class: str | None,
                error_category: ToolArgumentErrorCategory | None,
                error_message: str | None,
                post_version: int,
                _tool_outcomes: list[_ToolOutcome] = tool_outcomes,
                _tool_call: Any = tool_call,
                _pre_version: int = pre_version,
                _wire: _CallWireFacts = call_wire_facts,
                _state: CompositionState = state,
            ) -> None:
                if error_class is not None or (isinstance(response, ToolResult) and not response.success):
                    refused_resources.update(unavailable_resources(promises_by_call[_tool_call.id], _state))
                _tool_outcomes.append(
                    _ToolOutcome(
                        call=_tool_call,
                        response=response,
                        error_class=error_class,
                        error_category=error_category,
                        error_message=error_message,
                        pre_version=_pre_version,
                        post_version=post_version,
                        strict_sent=_wire.strict_sent,
                        wire_conformant=_wire.wire_conformant,
                    )
                )

            if mixed_abandonment_batch:
                detail = "cannot_complete must be the only tool call in its batch; every call in this batch was refused before effects."
                category = ToolArgumentErrorCategory.MODEL_VALIDATION
                rejection = ToolArgumentError(
                    argument="tool_calls",
                    expected="cannot_complete as the only call in its batch",
                    actual_type="mixed tool-call batch",
                    category=category,
                )
                payload = {"status": "ARG_ERROR", "error": detail, "error_category": category.value}
                audit = begin_dispatch(
                    tool_call.id,
                    tool_name,
                    tool_call.function.arguments,
                    version_before=state.version,
                    actor=actor,
                    strict_sent=call_wire_facts.strict_sent,
                    wire_conformant=None,
                )
                recorder.record_private_tool_authoring(private_authorings_by_call[tool_call.id])
                recorder.record(
                    finish_arg_error(
                        audit, error_class=type(rejection).__name__, error_category=category, error_message=detail, error_payload=payload
                    )
                )
                _append_tool_outcome(
                    response=None,
                    error_class=type(rejection).__name__,
                    error_category=category,
                    error_message=detail,
                    post_version=state.version,
                )
                tool_feedback_json = json.dumps(payload)
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_feedback_json,
                        PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                    }
                )
                turn_has_discovery = True
                all_cache_hits = False
                continue

            try:
                decoded_arguments = bounded_json_loads(tool_call.function.arguments, label=f"{tool_name} arguments")
            except (json.JSONDecodeError, JsonBoundaryError, TypeError, ValueError) as exc:
                # Track budget class even when args are unparseable.
                if is_discovery_tool(tool_name):
                    turn_has_discovery = True
                else:
                    turn_has_mutation = True
                # ARG_ERROR pre-dispatch site (1/3): JSON-decode failure.
                # Open the audit envelope with the raw (pre-parse) string
                # so the trail records what the LLM tried, even when it
                # wasn't valid JSON. ``error_message`` is class-name only
                # because ``str(exc)`` for JSONDecodeError can echo column
                # offsets that reference the un-truncated raw bytes.
                audit_arguments: Mapping[str, Any] | str = (
                    unknown_audit_arguments if unknown_audit_arguments is not None else tool_call.function.arguments
                )
                if unknown_audit_arguments is None:
                    replay_arguments = {
                        "_redaction_status": INVALID_TOOL_ARGUMENTS_REDACTION_STATUS,
                        "error_class": type(exc).__name__,
                    }
                    # Keep the original malformed wire bytes in the audit record
                    # for ordinary JSON decode failures, but send a valid JSON
                    # placeholder in the next provider request. The gateway's
                    # tool-call contract cannot replay malformed JSON arguments.
                    _replace_llm_tool_call_arguments(
                        llm_messages,
                        ctx=ctx,
                        call_model=call_model,
                        tool_call_id=tool_call.id,
                        arguments=replay_arguments,
                        dialect=ctx.tool_contract_dialect,
                        semantic=False,
                    )
                    if isinstance(exc, JsonBoundaryError):
                        audit_arguments = replay_arguments
                        decoded_args_by_call_id[tool_call.id] = dict(replay_arguments)
                # No arguments were decoded, so wire_conformant stays None.
                audit = begin_dispatch(
                    tool_call.id,
                    tool_name,
                    audit_arguments,
                    version_before=state.version,
                    actor=actor,
                    strict_sent=call_wire_facts.strict_sent,
                    wire_conformant=call_wire_facts.wire_conformant,
                )
                error_payload = {"error": f"Invalid JSON in arguments: {exc}"}
                # JsonBoundaryError is ELSPETH's own final-in-practice class (no
                # subclasses); the other three caught classes are decode failures.
                wire_category = (
                    ToolArgumentErrorCategory.WIRE_JSON_BOUNDS
                    if type(exc) is JsonBoundaryError
                    else ToolArgumentErrorCategory.WIRE_JSON_INVALID
                )
                recorder.record(
                    finish_arg_error(
                        audit,
                        error_class=type(exc).__name__,
                        error_category=wire_category,
                        error_message=type(exc).__name__,
                        error_payload=error_payload,
                    )
                )
                _append_tool_outcome(
                    response=None,
                    error_class=type(exc).__name__,
                    error_category=wire_category,
                    error_message=type(exc).__name__,
                    post_version=state.version,
                )
                anti_anchor.record_failure(tool_name, audit.arguments_hash)
                tool_feedback_json = json.dumps(error_payload)
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_feedback_json,
                        PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                    }
                )
                all_cache_hits = False
                continue

            # Copy receipt semantics before envelope rejection, redaction or
            # finalizer rebinding can replace them with a display projection.
            try:
                private_arguments_canonical = canonical_json(decoded_arguments)
            except (TypeError, ValueError) as exc:
                private_arguments_canonical = canonical_json(build_canonicalization_sentinel(exc, decoded_arguments))
            private_authorings_by_call[tool_call.id] = replace(
                private_authorings_by_call[tool_call.id], arguments_canonical=private_arguments_canonical
            )
            recorder.record_private_tool_authoring(private_authorings_by_call[tool_call.id])

            # Exact-type: bounded_json_loads is called with no object_hook /
            # object_pairs_hook, so json.loads builds a plain dict, and its own
            # _validate_decoded_json has already held every node to exactly
            # dict/list/str/bool/int/float/None. A Mapping that is not a dict
            # cannot reach this line.
            if type(decoded_arguments) is not dict:
                if is_discovery_tool(tool_name):
                    turn_has_discovery = True
                else:
                    turn_has_mutation = True
                # ARG_ERROR pre-dispatch site (2/3): non-dict arguments.
                # The LLM produced valid JSON but not a JSON object. The
                # canonicalized record wraps the (possibly scalar/list)
                # value under ``_decoded_non_object`` so the audit trail
                # captures it deterministically.
                if tool_was_sent:
                    call_wire_facts.wire_conformant = False
                audit, canonicalization_failed = begin_dispatch_or_arg_error(
                    tool_call.id,
                    tool_name,
                    unknown_audit_arguments if unknown_audit_arguments is not None else {"_decoded_non_object": decoded_arguments},
                    version_before=state.version,
                    actor=actor,
                    strict_sent=call_wire_facts.strict_sent,
                    wire_conformant=call_wire_facts.wire_conformant,
                )
                error_category: ToolArgumentErrorCategory
                if canonicalization_failed is None:
                    err_msg = f"Tool '{tool_name}' arguments must be a JSON object, got {type(decoded_arguments).__name__}."
                    not_object = _pre_dispatch_argument_error(tool_name, ToolArgumentErrorCategory.WIRE_NOT_OBJECT)
                    error_class = type(not_object).__name__
                    error_category = not_object.category
                    error_message = f"non-object arguments ({type(decoded_arguments).__name__})"
                else:
                    err_msg = f"Tool '{tool_name}' arguments are not canonical JSON ({type(canonicalization_failed).__name__})."
                    error_class = type(canonicalization_failed).__name__
                    error_category = ToolArgumentErrorCategory.CANONICALIZATION
                    error_message = type(canonicalization_failed).__name__
                error_payload = {"error": err_msg}
                recorder.record(
                    finish_arg_error(
                        audit,
                        error_class=error_class,
                        error_category=error_category,
                        error_message=error_message,
                        error_payload=error_payload,
                    )
                )
                _append_tool_outcome(
                    response=None,
                    error_class=error_class,
                    error_category=error_category,
                    error_message=error_message,
                    post_version=state.version,
                )
                anti_anchor.record_failure(tool_name, audit.arguments_hash)
                tool_feedback_json = json.dumps(error_payload)
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_feedback_json,
                        PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                    }
                )
                all_cache_hits = False
                continue

            if tool_was_sent:
                # Decode the provider arguments against the W that was sent:
                # classify wire conformance, unwrap the set_pipeline envelope, and
                # on openai_strict strip ``null`` at promoted positions. A
                # malformed envelope or no-argument marker rejects before S.
                try:
                    decoded = decode_wire_arguments(tool_name, ctx.tool_contract_dialect, decoded_arguments)
                except ToolArgumentError as envelope_rejection:
                    call_wire_facts.wire_conformant = False
                    if is_discovery_tool(tool_name):
                        turn_has_discovery = True
                    else:
                        turn_has_mutation = True
                    audit_arguments = {
                        "_redaction_status": INVALID_TOOL_ARGUMENTS_REDACTION_STATUS,
                        "error_class": type(envelope_rejection).__name__,
                    }
                    decoded_args_by_call_id[tool_call.id] = dict(audit_arguments)
                    _replace_llm_tool_call_arguments(
                        llm_messages,
                        ctx=ctx,
                        call_model=call_model,
                        tool_call_id=tool_call.id,
                        arguments=audit_arguments,
                        dialect=ctx.tool_contract_dialect,
                        semantic=False,
                    )
                    audit = begin_dispatch(
                        tool_call.id,
                        tool_name,
                        audit_arguments,
                        version_before=state.version,
                        actor=actor,
                        strict_sent=call_wire_facts.strict_sent,
                        wire_conformant=call_wire_facts.wire_conformant,
                    )
                    envelope_error = wire_argument_repair_message(sent_wire_tools[tool_name])
                    error_payload = {"error": envelope_error}
                    recorder.record(
                        finish_arg_error(
                            audit,
                            error_class=type(envelope_rejection).__name__,
                            error_category=envelope_rejection.category,
                            error_message=envelope_error,
                            error_payload=error_payload,
                        )
                    )
                    _append_tool_outcome(
                        response=None,
                        error_class=type(envelope_rejection).__name__,
                        error_category=envelope_rejection.category,
                        error_message=envelope_error,
                        post_version=state.version,
                    )
                    anti_anchor.record_failure(tool_name, audit.arguments_hash)
                    tool_feedback_json = json.dumps(error_payload)
                    llm_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_feedback_json,
                            PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                        }
                    )
                    all_cache_hits = False
                    continue
                # From here on ``arguments`` is the decoded (semantic) form: S is
                # the contract every later gate, handler and validator reads.
                arguments = cast(dict[str, Any], deep_thaw(decoded.semantic))
                call_wire_facts.wire_conformant = decoded.wire_conformant
            else:
                arguments = cast(dict[str, Any], decoded_arguments)
            _retain_llm_tool_argument_parts(llm_messages, tool_call_id=tool_call.id, ctx=ctx, call_model=call_model)
            try:
                private_arguments_canonical = canonical_json(arguments)
                if tool_name == "set_pipeline":
                    private_authority_canonical = composer_authority_canonical_json(arguments)
            except (TypeError, ValueError) as exc:
                private_arguments_canonical = canonical_json(build_canonicalization_sentinel(exc, arguments))
                private_authority_canonical = None
            private_authorings_by_call[tool_call.id] = replace(
                private_authorings_by_call[tool_call.id],
                arguments_canonical=private_arguments_canonical,
                authority_arguments_canonical=private_authority_canonical,
            )
            recorder.record_private_tool_authoring(private_authorings_by_call[tool_call.id])
            if unknown_audit_arguments is not None:
                audit_arguments = unknown_audit_arguments
            elif tool_name == "set_pipeline":
                audit_arguments = cast(dict[str, Any], inline_custody_audit_projection(arguments))
            else:
                audit_arguments = arguments
            decoded_args_by_call_id[tool_call.id] = audit_arguments
            promises_by_call[tool_call.id] = promised_resources(tool_name, arguments)

            # Open the audit envelope ONCE per dispatch — the cache,
            # required-paths, ToolArgumentError, plugin-crash, and
            # success branches below all read from this envelope so
            # ``started_at``/``arguments_canonical``/``version_before``
            # are consistent regardless of which branch fires.
            audit, canonicalization_failed = begin_dispatch_or_arg_error(
                tool_call.id,
                tool_name,
                audit_arguments,
                version_before=state.version,
                actor=actor,
                strict_sent=call_wire_facts.strict_sent,
                wire_conformant=call_wire_facts.wire_conformant,
            )
            if audit_arguments is not arguments:
                _replace_llm_tool_call_arguments(
                    llm_messages,
                    ctx=ctx,
                    call_model=call_model,
                    tool_call_id=tool_call.id,
                    arguments=audit_arguments,
                    dialect=ctx.tool_contract_dialect,
                    semantic=True,
                )
            if canonicalization_failed is not None:
                if is_discovery_tool(tool_name):
                    turn_has_discovery = True
                else:
                    turn_has_mutation = True
                error_payload = {
                    "error": f"Tool '{tool_name}' arguments are not canonical JSON ({type(canonicalization_failed).__name__})."
                }
                recorder.record(
                    finish_arg_error(
                        audit,
                        error_class=type(canonicalization_failed).__name__,
                        error_category=ToolArgumentErrorCategory.CANONICALIZATION,
                        error_message=type(canonicalization_failed).__name__,
                        error_payload=error_payload,
                    )
                )
                _append_tool_outcome(
                    response=None,
                    error_class=type(canonicalization_failed).__name__,
                    error_category=ToolArgumentErrorCategory.CANONICALIZATION,
                    error_message=type(canonicalization_failed).__name__,
                    post_version=state.version,
                )
                anti_anchor.record_failure(tool_name, audit.arguments_hash)
                tool_feedback_json = json.dumps(error_payload)
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_feedback_json,
                        PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                    }
                )
                all_cache_hits = False
                continue

            call.tool_name = tool_name
            call.private_raw_arguments = private_raw_arguments
            call.private_arguments_canonical = private_arguments_canonical
            call.private_authority_canonical = private_authority_canonical
            call.unknown_audit_arguments = unknown_audit_arguments
            call.arguments = arguments
            call.audit_arguments = audit_arguments
            call.audit = audit
            call.phase = "prepared"
            if ctx.session_operation_context is not None:
                call.original_creator_id = ctx.session_operation_context.fence.operation_id
                call.original_creator_epoch = ctx.session_operation_context.fence.operation_epoch
            prepared = True
    finally:
        batch.state = state
        batch.last_validation = last_validation
        batch.last_runtime_preflight = last_runtime_preflight
        batch.advisor_calls_used = advisor_calls_used
        batch.turn_has_mutation = turn_has_mutation
        batch.turn_has_discovery = turn_has_discovery
        batch.all_cache_hits = all_cache_hits
        batch.tool_outcomes = tool_outcomes
        batch.plugin_crash = plugin_crash
        batch.plugin_crash_cause = plugin_crash_cause
        batch.advisor_failure = advisor_failure
        batch.service_failure = service_failure
        batch.advisor_compose_timeout = advisor_compose_timeout
        batch.pre_state_id = pre_state_id
        batch.decoded_args_by_call_id = decoded_args_by_call_id
        batch.private_authorings_by_call = private_authorings_by_call
        batch.proposals_this_turn = proposals_this_turn
        batch.mutation_success_observed = mutation_success_observed
        batch.refused_resources = refused_resources
        batch.promises_by_call = promises_by_call
    if prepared:
        return "prepared"
    if len(tool_outcomes) == before + 1:
        call.phase = "applied"
        return "applied"
    if len(tool_outcomes) != before:
        raise AuditIntegrityError("tool preparation produced more than one actual outcome")
    batch.stop_issued_batch = True
    return "stop"''',
    "batch._execute_continuation_call_live": r'''async def _execute_continuation_call_live(
    *,
    call_model: _CallModelOutcome,
    ctx: ToolBatchContext,
    acc: BatchAccumulator,
    llm_messages: list[dict[str, Any]],
    batch: ContinuationBatchState,
    call: ContinuationCallState,
    retained_attachment: AttachmentResultCheckpoint | None = None,
    failure_custody: ContinuationToolFailureCustody | None = None,
) -> Literal["applied"]:
    """Execute the original suffix with the actual caller's revocation owner."""
    if retained_attachment is not None:
        from elspeth.web.sessions.continuation_attachment_result import AttachmentResultCheckpoint

        if (
            type(retained_attachment) is not AttachmentResultCheckpoint
            or not retained_attachment.history_applied
            or retained_attachment.prepared.batch is not batch
            or retained_attachment.prepared.accumulator is not acc
            or batch.current_call is not call
            or call.tool_name not in ("read_attachment", "get_blob_content")
            or retained_attachment.result.updated_state is not call.pre_state
            or call_model.completion.tool_batch.calls != retained_attachment.prepared.calls
        ):
            raise AuditIntegrityError("retained attachment execution changed its actual selected read graph")
    recorder = ctx.recorder
    observation = None if failure_custody is None else failure_custody.observation
    if failure_custody is not None and observation is not None:
        if call.audit is None or failure_custody.batch is not batch or failure_custody.call is not call:
            raise AuditIntegrityError("issued failure custody changed its actual batch/call")
        observation.require(recorder, call.audit)
    anti_anchor = ctx.anti_anchor
    discovery_cache = ctx.discovery_cache
    runtime_preflight_cache = ctx.runtime_preflight_cache
    session_id = ctx.session_id
    user_id = ctx.user_id
    user_message_id = ctx.user_message_id
    user_message_content = ctx.user_message_content
    current_state_id = ctx.current_state_id
    initial_version = ctx.initial_version
    deadline = ctx.deadline
    progress = ctx.progress
    session_scope = ctx.session_scope
    turn_sessions_service = ctx.turn_sessions_service
    turn_session_uuid = ctx.turn_session_uuid
    state = batch.state
    last_validation = batch.last_validation
    last_runtime_preflight = batch.last_runtime_preflight
    advisor_calls_used = batch.advisor_calls_used
    turn_has_mutation = batch.turn_has_mutation
    turn_has_discovery = batch.turn_has_discovery
    all_cache_hits = batch.all_cache_hits
    tool_outcomes = batch.tool_outcomes
    plugin_crash = batch.plugin_crash
    plugin_crash_cause = batch.plugin_crash_cause
    advisor_failure = batch.advisor_failure
    service_failure = batch.service_failure
    advisor_compose_timeout = batch.advisor_compose_timeout
    pre_state_id = batch.pre_state_id
    decoded_args_by_call_id = batch.decoded_args_by_call_id
    private_authorings_by_call = batch.private_authorings_by_call
    proposals_this_turn = batch.proposals_this_turn
    mutation_success_observed = batch.mutation_success_observed
    refused_resources = batch.refused_resources
    promises_by_call = batch.promises_by_call
    provider_model_version = batch.provider_model_version
    from elspeth.web.composer.redaction import redact_tool_call_arguments

    if call.audit is None or call.arguments is None or call.audit_arguments is None:
        raise AuditIntegrityError("tool execution preceded its retained prepared dispatch")
    tool_name = call.tool_name
    arguments = call.arguments
    audit_arguments = call.audit_arguments
    audit = call.audit
    before = len(tool_outcomes)
    ran_to_end = False
    from elspeth.web.composer.continuation_prompt_producers import ContinuationToolMessagePublication

    publication = None
    if ctx.message_producers is not None:
        ctx.message_producers.require_context(session_id=session_id, context=ctx.session_operation_context, actor_user_id=user_id)
        publication = ContinuationToolMessagePublication(
            ctx.message_producers, call_model.completion, batch, call, recorder, llm_messages, ctx.policy_catalog
        )

    def _append_issued_feedback(serialized: str) -> None:
        message = {
            "role": "tool",
            "tool_call_id": call.tool_call.id,
            "content": serialized,
            PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(serialized),
        }
        if publication is not None:
            publication.register_feedback(serialized=serialized, message=message)
        llm_messages.append(message)

    def _append_tool_outcome(
        *,
        response: Any,
        error_class: str | None,
        error_category: ToolArgumentErrorCategory | None,
        error_message: str | None,
        post_version: int,
        _batch: ContinuationBatchState = batch,
        _call: ContinuationCallState = call,
    ) -> None:
        _append_continuation_tool_outcome(
            _batch,
            _call,
            response=response,
            error_class=error_class,
            error_category=error_category,
            error_message=error_message,
            post_version=post_version,
        )

    def _complete_service_failure(original: _ServiceFailure) -> None:
        nonlocal service_failure, all_cache_hits
        from elspeth.web.composer.continuation_tool_failure import combine_failure_objects

        if failure_custody is not None:
            failure_custody.observe_primary(original)
            if (
                observation is None
                or not observation.execution_failed
                or observation.returned
                or failure_custody.errors
                or failure_custody.original is not original
            ):
                raise failure_custody.failure(original)
        payload = {"success": False, "status": "SERVICE_FAILURE", "reason": "service_failure"}
        try:
            invocation = finish_success(audit, result_payload=payload, version_after=state.version)
            if observation is None:
                recorder.record(invocation)
            else:
                recorder.record_with_failure_observation(invocation, observation)
            _append_tool_outcome(response=payload, error_class=None, error_category=None, error_message=None, post_version=state.version)
            if failure_custody is not None:
                failure_custody.promote_completed_service_failure(original)
        except BaseException as secondary:
            if failure_custody is not None:
                combined = failure_custody.failure(secondary)
                if combined is secondary:
                    raise
                raise combined from combined.__cause__
            combined = combine_failure_objects(original, secondary)
            if combined is secondary:
                raise
            raise combined from combined.__cause__
        service_failure = original
        batch.service_failure = original
        all_cache_hits = False

    try:
        for tool_call in (call.tool_call,):
            # A refused producer cannot supply a new prerequisite to another call
            # authored in this same response. Existing saved resources remain usable.
            if is_mutation_tool(tool_name) and has_unavailable_prerequisite(tool_name, arguments, refused_resources, state):
                reject = _candidate_rejection_result if tool_name == "set_pipeline" else _saved_graph_rejection_result
                blocked = reject(
                    state,
                    "This mutation depends on a new resource or connection from an earlier refused call in this batch. "
                    "Nothing from this call was applied. Inspect the saved pipeline and repair the refused prerequisite "
                    "in a new turn before retrying this dependent change.",
                    error_code="batch_dependency_refused",
                    rejected_component=None,
                )
                blocked = finalize_tool_result(
                    blocked,
                    tool_name=tool_name,
                    catalog=ctx.policy_catalog,
                    context=ToolContext(catalog=ctx.policy_catalog, plugin_snapshot=ctx.plugin_snapshot),
                    prior_validation=last_validation or ctx.policy_catalog.validate_composition_state(state).validation,
                )
                recorder.record(finish_success(audit, result_payload=blocked.to_dict(), version_after=state.version))
                _append_tool_outcome(
                    response=blocked, error_class=None, error_category=None, error_message=None, post_version=state.version
                )
                anti_anchor.record_failure(tool_name, audit.arguments_hash)
                tool_feedback_json = _serialize_tool_result(blocked)
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_feedback_json,
                        PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                    }
                )
                await emit_progress(progress, tool_completed_progress_event(tool_name, False))
                turn_has_mutation = True
                all_cache_hits = False
                continue

            # Check discovery cache before executing
            if is_cacheable_discovery_tool(tool_name):
                cache_key = _make_cache_key(tool_name, arguments)
                if cache_key in discovery_cache:
                    # Cache hit — return cached result, no budget charge.
                    # Audit-recorded with cache_hit=True so the trail
                    # captures every LLM decision-point, even those
                    # served from cache without re-running the handler.
                    await _await_started_tool_stage(
                        emit_progress(
                            progress,
                            ComposerProgressEvent(
                                phase="using_tools",
                                headline="I'm reusing recently checked tool information.",
                                evidence=("The same discovery request was already answered for this compose step.",),
                                likely_next="ELSPETH will continue from the cached tool result.",
                            ),
                        ),
                        audit=audit,
                        recorder=recorder,
                        failure_observation=observation,
                    )
                    try:
                        cached_admitted = (
                            admitted_result_from_cached_discovery_payload(tool_name, state, discovery_cache[cache_key])
                            if response_contract_for(tool_name) is not None
                            else None
                        )
                        cached_result = (
                            cached_admitted.result
                            if cached_admitted is not None
                            else _result_from_cached_discovery_payload(state, discovery_cache[cache_key])
                        )
                        cached_result = normalize_tool_result_validation(cached_result, ctx.policy_catalog)
                        if cached_admitted is not None:
                            cached_admitted = AdmittedDiscoveryResult(cached_result, cached_admitted.response, cached_admitted.contract)
                        cached_payload = {
                            "success": cached_result.success,
                            "data": cached_admitted.response.to_wire()
                            if cached_admitted is not None and cached_admitted.response is not None
                            else cached_result.data,
                            "cache_hit": True,
                        }
                        cached_outcome = cached_admitted.to_tool_result() if cached_admitted is not None else cached_result
                        cached_json = (
                            serialize_admitted_discovery_result(cached_admitted)
                            if cached_admitted is not None
                            else _serialize_tool_result(cached_result)
                        )
                    except Exception as cache_exc:
                        recorder.record(finish_plugin_crash(audit, exc=cache_exc))
                        raise
                    recorder.record(
                        finish_success(
                            audit,
                            result_payload=cached_payload,
                            version_after=state.version,
                            cache_hit=True,
                        )
                    )
                    _append_tool_outcome(
                        response=cached_outcome,
                        error_class=None,
                        error_category=None,
                        error_message=None,
                        post_version=state.version,
                    )
                    # Cache hits are exclusively for discovery tools
                    # (`is_cacheable_discovery_tool` gates above). A
                    # discovery success is an *observation* — the model
                    # gained schema knowledge but did not change state.
                    # The §7.7 anchor is broken only by mutation
                    # progress, so tracker stays untouched here.
                    cached_message = tool_result_message(
                        call_id=tool_call.id,
                        result=cached_admitted if cached_admitted is not None else cached_result,
                        serialized=cached_json,
                    )
                    if ctx.message_producers is not None and tool_name == "get_plugin_schema":
                        ctx.message_producers.require_context(
                            session_id=session_id, context=ctx.session_operation_context, actor_user_id=user_id
                        )
                        schema_admitted = (
                            cached_admitted if cached_admitted is not None else admit_discovery_result(tool_name, cached_result)
                        )
                        ctx.message_producers.register_schema_message(call=tool_call, admitted=schema_admitted, message=cached_message)
                    llm_messages.append(cached_message)
                    continue

            all_cache_hits = False

            # Validate schema-declared required arguments at the
            # Tier 3 boundary BEFORE entering tool handler code.
            # This walks nested object/array schemas, so malformed
            # set_pipeline payloads like source.plugin omissions are
            # caught here; any KeyError that still escapes
            # execute_tool() is an internal bug and must crash.
            # Unknown tool names skip validation — execute_tool()
            # handles them with a failure result downstream.
            required_paths = _TOOL_REQUIRED_PATHS[tool_name] if tool_name in _TOOL_REQUIRED_PATHS else ()
            missing = _find_missing_required_paths(arguments, required_paths)
            if missing:
                if is_discovery_tool(tool_name):
                    turn_has_discovery = True
                else:
                    turn_has_mutation = True
                # ARG_ERROR pre-dispatch site (3/3): schema-required
                # paths missing. ``missing`` is a list of dotted/indexed
                # path strings — operator-controlled schema field names,
                # safe to echo verbatim.
                err_msg = f"Tool '{tool_name}' missing required argument(s): {', '.join(missing)}"
                error_payload = {"error": err_msg}
                missing_paths = _pre_dispatch_argument_error(tool_name, ToolArgumentErrorCategory.MISSING_REQUIRED_PATH)
                recorder.record(
                    finish_arg_error(
                        audit,
                        error_class=type(missing_paths).__name__,
                        error_category=missing_paths.category,
                        error_message=f"missing: {', '.join(missing)}",
                        error_payload=error_payload,
                    )
                )
                _append_tool_outcome(
                    response=None,
                    error_class=type(missing_paths).__name__,
                    error_category=missing_paths.category,
                    error_message=f"missing: {', '.join(missing)}",
                    post_version=state.version,
                )
                anti_anchor.record_failure(tool_name, audit.arguments_hash)
                tool_feedback_json = json.dumps(error_payload)
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_feedback_json,
                        PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                    }
                )
                continue

            prevalidated_unapplied_result: ToolResult | None = None
            preproposal_exception: BaseException | None = None
            candidate_prior_validation: ValidationSummary | None = None
            candidate_context: ToolContext | None = None
            candidate: Any = None
            pipeline_custody_result: Literal["not_required", "ready"] = "not_required"
            interpretation_requirements_are_internal = False
            explicit_approval_required = (
                ctx.review_required
                and is_mutation_tool(tool_name)
                # create_blob stays immediate because it allocates the blob id a
                # later composition proposal may reference. Destructive blob-only
                # writes such as update_blob/delete_blob still require approval.
                and (not is_blob_store_only_mutation_tool(tool_name) or is_approval_required_blob_store_only_mutation_tool(tool_name))
            )
            auto_mutation = (
                ctx.mutation_ceiling
                if (
                    not explicit_approval_required
                    and is_mutation_tool(tool_name)
                    and (not is_blob_store_only_mutation_tool(tool_name) or is_approval_required_blob_store_only_mutation_tool(tool_name))
                )
                else None
            )

            # The freeform compose loop is a second authoring surface beside the
            # shared planner. Its atomic set_pipeline call is the equivalent
            # terminal candidate seam: validate the provider draft first, then
            # splice deployment-REQUIRED controls exactly once before proposal
            # custody or auto-commit. Incomplete/invalid drafts remain byte-exact
            # and continue through the ordinary repair path; in particular, the
            # server never launders a provider-authored reserved disclosure into
            # an internal one merely by attempting finalization.
            if tool_name == "set_pipeline" and not explicit_approval_required:
                try:
                    candidate_prior_validation = (
                        last_validation if last_validation is not None else ctx.policy_catalog.validate_composition_state(state).validation
                    )
                    candidate_context = ToolContext(
                        catalog=ctx.policy_catalog,
                        plugin_snapshot=ctx.plugin_snapshot,
                        data_dir=ctx.custody.data_dir,
                        require_data_dir_for_paths=True,
                        session_engine=ctx.custody.session_engine,
                        session_id=session_id,
                        session_operation_context=ctx.session_operation_context,
                        session_operation_authority=ctx.session_operation_authority,
                        secret_service=ctx.custody.secret_service,
                        secret_wiring_policy=ctx.custody.secret_wiring_policy,
                        user_id=user_id,
                        current_validation=candidate_prior_validation,
                        max_blob_storage_per_session_bytes=ctx.custody.max_blob_storage_per_session_bytes,
                        user_message_id=user_message_id,
                        user_message_content=user_message_content,
                        composer_model_identifier=ctx.provenance.model_identifier,
                        composer_model_version=provider_model_version,
                        composer_provider=ctx.provenance.provider or "unknown",
                        composer_skill_hash=ctx.provenance.skill_hash,
                        tool_arguments_hash=audit.binding_arguments_hash,
                        mutation_ceiling=auto_mutation,
                    )
                    finalization = await _finalize_complete_set_pipeline_candidate(
                        arguments,
                        state,
                        candidate_context,
                        plugin_snapshot=ctx.plugin_snapshot,
                        policy_catalog=ctx.policy_catalog,
                    )
                    arguments = cast(dict[str, Any], finalization.arguments)
                    candidate_context = finalization.context
                    candidate = finalization.candidate
                    if finalization.changed:
                        audit_arguments = cast(dict[str, Any], inline_custody_audit_projection(arguments))
                        audit = rebind_dispatch_arguments(audit, audit_arguments, failure_observation=observation)
                        decoded_args_by_call_id[tool_call.id] = audit_arguments
                        transcript_arguments = (
                            redact_tool_call_arguments(tool_name, arguments, telemetry=ctx.redaction_telemetry)
                            if candidate.prepared_inline_blob is not None
                            else audit_arguments
                        )
                        _replace_llm_tool_call_arguments(
                            llm_messages,
                            ctx=ctx,
                            call_model=call_model,
                            tool_call_id=tool_call.id,
                            arguments=transcript_arguments,
                            dialect=ctx.tool_contract_dialect,
                            semantic=True,
                        )
                        interpretation_requirements_are_internal = True
                except BaseException as exc:
                    # Re-raise exactly once from inside dispatch_with_audit so the
                    # established ARG_ERROR / PLUGIN_CRASH authority records the
                    # failure without any proposal, blob, or state publication.
                    preproposal_exception = exc

            if explicit_approval_required:
                assert turn_sessions_service is not None
                assert turn_session_uuid is not None
                if proposals_this_turn >= _MAX_PENDING_PROPOSALS_PER_TURN:
                    # Shadowed by the batch preflight above (same population, same
                    # cap); kept so a later change to either count cannot open an
                    # unbounded proposal turn.
                    raise _pending_proposal_cap_error(proposals_this_turn + 1, ctx=ctx, state=state)

                from pydantic import ValidationError as PydanticValidationError

                # The LLM may produce arguments that fail the redaction
                # MANIFEST's argument_model — most commonly a misplaced
                # field like ``nodes[*].schema`` belonging at
                # ``nodes[*].options.schema``. The runtime validator
                # would reject these the same way. On redaction
                # ValidationError we fall through to normal dispatch:
                # execute_tool emits a clean ToolArgumentError, the
                # existing post-dispatch arg-error handling records
                # the failure for audit (with the
                # ``_redaction_status: invalid_tool_arguments`` marker),
                # and the compose loop continues so the model can
                # self-correct. The previous behaviour — letting the
                # ValidationError propagate — crashed the compose
                # request with HTTP 500 (session 100dc5cb… 2026-05-14:
                # frontend rendered a generic ApiError as a bare
                # "retry" button with no diagnostic).
                redacted_arguments: Mapping[str, Any] | None
                try:
                    redacted_arguments = redact_tool_call_arguments(
                        tool_name,
                        arguments,
                        telemetry=ctx.redaction_telemetry,
                    )
                except PydanticValidationError:
                    redacted_arguments = None

                proposal_tool_name = tool_name
                proposal_arguments: Mapping[str, Any] = arguments
                proposal_summary_arguments: Mapping[str, Any] = arguments
                proposal_redacted_arguments = redacted_arguments

                if redacted_arguments is not None and tool_name == "set_pipeline":
                    if preproposal_exception is not None:
                        redacted_arguments = None
                    else:
                        try:
                            candidate_prior_validation = (
                                last_validation
                                if last_validation is not None
                                else ctx.policy_catalog.validate_composition_state(state).validation
                            )
                            candidate_context = ToolContext(
                                catalog=ctx.policy_catalog,
                                plugin_snapshot=ctx.plugin_snapshot,
                                data_dir=ctx.custody.data_dir,
                                require_data_dir_for_paths=True,
                                session_engine=ctx.custody.session_engine,
                                session_id=session_id,
                                session_operation_context=ctx.session_operation_context,
                                session_operation_authority=ctx.session_operation_authority,
                                secret_service=ctx.custody.secret_service,
                                secret_wiring_policy=ctx.custody.secret_wiring_policy,
                                user_id=user_id,
                                current_validation=candidate_prior_validation,
                                max_blob_storage_per_session_bytes=ctx.custody.max_blob_storage_per_session_bytes,
                                user_message_id=user_message_id,
                                user_message_content=user_message_content,
                                composer_model_identifier=ctx.provenance.model_identifier,
                                composer_model_version=provider_model_version,
                                composer_provider=ctx.provenance.provider or "unknown",
                                composer_skill_hash=ctx.provenance.skill_hash,
                                tool_arguments_hash=audit.binding_arguments_hash,
                            )
                            finalization = await _finalize_complete_set_pipeline_candidate(
                                arguments,
                                state,
                                candidate_context,
                                plugin_snapshot=ctx.plugin_snapshot,
                                policy_catalog=ctx.policy_catalog,
                            )
                            arguments = cast(dict[str, Any], finalization.arguments)
                            candidate_context = finalization.context
                            candidate = finalization.candidate
                            if finalization.changed:
                                audit_arguments = cast(dict[str, Any], inline_custody_audit_projection(arguments))
                                audit = rebind_dispatch_arguments(audit, audit_arguments, failure_observation=observation)
                                decoded_args_by_call_id[tool_call.id] = audit_arguments
                                redacted_arguments = redact_tool_call_arguments(
                                    tool_name,
                                    arguments,
                                    telemetry=ctx.redaction_telemetry,
                                )
                                _replace_llm_tool_call_arguments(
                                    llm_messages,
                                    ctx=ctx,
                                    call_model=call_model,
                                    tool_call_id=tool_call.id,
                                    arguments=redacted_arguments if candidate.prepared_inline_blob is not None else audit_arguments,
                                    dialect=ctx.tool_contract_dialect,
                                    semantic=True,
                                )
                                interpretation_requirements_are_internal = True
                            finalized_candidate_result = finalize_tool_result(
                                candidate.result,
                                tool_name=tool_name,
                                catalog=ctx.policy_catalog,
                                context=candidate_context,
                                prior_validation=candidate_prior_validation,
                            )
                            proposal_acceptable = candidate.acceptable
                            if proposal_acceptable and candidate.prepared_inline_blob is not None:
                                if session_id is None or ctx.custody.session_engine is None:
                                    raise AuditIntegrityError("Inline proposal custody requires session context")
                                custody = prepare_pipeline_custody(
                                    arguments,
                                    candidate.prepared_inline_blob,
                                    session_id=session_id,
                                    max_storage_per_session=ctx.custody.max_blob_storage_per_session_bytes,
                                )

                                # From this point forward every authority-bearing
                                # and externally visible copy uses source.blob_id.
                                arguments = cast(dict[str, Any], deep_thaw(custody.arguments))
                                audit = rebind_dispatch_arguments(audit, arguments, failure_observation=observation)
                                decoded_args_by_call_id[tool_call.id] = arguments
                                # Authority retains the exact review draft. Only its
                                # redacted projection belongs in provider history.
                                redacted_arguments = redact_tool_call_arguments(
                                    tool_name,
                                    arguments,
                                    telemetry=ctx.redaction_telemetry,
                                )
                                _replace_llm_tool_call_arguments(
                                    llm_messages,
                                    ctx=ctx,
                                    call_model=call_model,
                                    tool_call_id=tool_call.id,
                                    arguments=redacted_arguments,
                                    dialect=ctx.tool_contract_dialect,
                                    semantic=True,
                                )
                                safe_candidate_context = replace(
                                    candidate_context,
                                    tool_arguments_hash=audit.binding_arguments_hash,
                                )
                                custody_outcome = await _try_finalize_proposal_custody(
                                    custody,
                                    engine=ctx.custody.session_engine,
                                    data_dir=ctx.custody.data_dir,
                                    max_storage_per_session=ctx.custody.max_blob_storage_per_session_bytes,
                                    session_operation_context=ctx.session_operation_context,
                                    session_operation_authority=ctx.session_operation_authority,
                                )
                                if custody_outcome == "quota_exceeded":
                                    proposal_acceptable = False
                                    finalized_candidate_result = finalize_tool_result(
                                        _candidate_rejection_result(
                                            state,
                                            "Session blob quota exceeded while reserving inline proposal custody.",
                                            error_code="BLOB_QUOTA_EXCEEDED",
                                            rejected_component="source",
                                        ),
                                        tool_name=tool_name,
                                        catalog=ctx.policy_catalog,
                                        context=safe_candidate_context,
                                        prior_validation=candidate_prior_validation,
                                    )
                                else:
                                    pipeline_custody_result = "ready"
                                    candidate = await run_sync_in_worker(
                                        build_set_pipeline_candidate,
                                        arguments,
                                        state,
                                        safe_candidate_context,
                                    )
                                    finalized_candidate_result = finalize_tool_result(
                                        candidate.result,
                                        tool_name=tool_name,
                                        catalog=ctx.policy_catalog,
                                        context=safe_candidate_context,
                                        prior_validation=candidate_prior_validation,
                                    )
                                    proposal_acceptable = candidate.acceptable

                        except BaseException as exc:
                            # Candidate finalization is one-time pre-proposal work.
                            # Re-raise it inside dispatch_with_audit below without
                            # rerunning the operation.
                            preproposal_exception = exc
                            redacted_arguments = None
                        else:
                            if not proposal_acceptable:
                                # ToolResult.data is deep-frozen by __post_init__, so
                                # the membership test must accept a mappingproxy. See
                                # _prevalidation_feedback_seed for the full contract.
                                feedback_data = dict(_prevalidation_feedback_seed(finalized_candidate_result.data))
                                feedback_data.update(
                                    _PrevalidationRejectedStatus(
                                        status="PREVALIDATION_REJECTED",
                                        applied=False,
                                        candidate_version=finalized_candidate_result.updated_state.version,
                                        message=(
                                            "The candidate pipeline failed prevalidation, was not applied, and was not "
                                            "submitted for approval. Repair the reported validation errors and retry."
                                        ),
                                    )
                                )
                                # ``updated_state=state``: nothing was applied, so the
                                # envelope's ``version`` — which the skill teaches as
                                # "the state version after the call" — must be the
                                # unapplied one. Keeping the candidate's state made the
                                # wire say the mutation landed while the audit
                                # (``_version_after``), the loop (which refuses the
                                # candidate state) and ``_append_tool_outcome`` all
                                # recorded that it had not (systems seat SYS-R3-3).
                                # Contained to this result: it reaches only
                                # ``_do_dispatch`` -> ``dispatch_with_audit`` ->
                                # ``_serialize_tool_result`` / ``_append_tool_outcome``,
                                # and ``pipeline_commit`` builds its own candidate from
                                # the proposal rather than reading this one.
                                #
                                # ``prior_validation=None`` and ``affected_nodes=()``
                                # for the same reason: both describe CHANGES, and
                                # nothing changed. ``to_dict`` emits
                                # ``validation_delta`` only when ``prior_validation``
                                # is set, and the delta it emitted was measurably
                                # false — on a real compose loop it reported
                                # ``resolved_errors`` naming the two errors the
                                # unapplied state still had, next to a ``version``
                                # saying nothing was applied, while the skill tells
                                # the model to act on the delta and never re-read
                                # state to check it (LLM seat LLM-R3B-1). The
                                # candidate's own ``affected_nodes`` said the same
                                # thing in the other direction: components touched by
                                # a state that was discarded. ``validation`` stays the
                                # candidate's — it is the rejection the model repairs
                                # from, and the skill says whose it is. This is now
                                # the APPROVAL_REQUIRED envelope's shape on the same
                                # path: unapplied state, empty affected_nodes, no
                                # delta.
                                prevalidated_unapplied_result = replace(
                                    finalized_candidate_result,
                                    data=feedback_data,
                                    updated_state=state,
                                    prior_validation=None,
                                    affected_nodes=(),
                                )
                                # Route the rejected result through canonical
                                # dispatch/outcome without proposal publication.
                                redacted_arguments = None

                proposal_redacted_arguments = redacted_arguments
                if redacted_arguments is not None and tool_name in _MUTATION_TOOLS and tool_name != "set_pipeline":
                    try:
                        preview_result = await run_sync_in_worker(
                            execute_tool,
                            tool_name,
                            arguments,
                            state,
                            ctx.policy_catalog,
                            plugin_snapshot=ctx.plugin_snapshot,
                            data_dir=ctx.custody.data_dir,
                            session_engine=ctx.custody.session_engine,
                            session_id=session_id,
                            session_operation_context=ctx.session_operation_context,
                            session_operation_authority=ctx.session_operation_authority,
                            secret_service=ctx.custody.secret_service,
                            secret_wiring_policy=ctx.custody.secret_wiring_policy,
                            user_id=user_id,
                            prior_validation=last_validation,
                            runtime_preflight=None,
                            max_blob_storage_per_session_bytes=ctx.custody.max_blob_storage_per_session_bytes,
                            user_message_id=user_message_id,
                            user_message_content=user_message_content,
                            composer_model_identifier=ctx.provenance.model_identifier,
                            composer_model_version=provider_model_version,
                            composer_provider=ctx.provenance.provider or "unknown",
                            composer_skill_hash=ctx.provenance.skill_hash,
                            tool_arguments_hash=audit.binding_arguments_hash,
                            validate_arguments=True,
                            require_data_dir_for_paths=True,
                            raise_schema_argument_errors=True,
                        )
                        if (
                            preview_result.success
                            and preview_result.updated_state.version > state.version
                            and preview_result.validation.is_valid
                        ):
                            finalized_state = await run_sync_in_worker(
                                wire_required_controls_state,
                                preview_result.updated_state,
                                ctx.plugin_snapshot,
                                ctx.policy_catalog,
                            )
                            if finalized_state is not preview_result.updated_state:
                                finalized_validation = await run_sync_in_worker(
                                    ctx.policy_catalog.validate_composition_state,
                                    finalized_state,
                                )
                                if not finalized_validation.validation.is_valid:
                                    raise AuditIntegrityError("Required-control proposal finalization produced an invalid composition")
                                proposal_arguments = owned_composition_state_authority(finalized_validation.authored_state)
                                proposal_summary_arguments = owned_composition_state_review_arguments(proposal_arguments)
                                proposal_tool_name = "set_pipeline"
                                proposal_redacted_arguments = redact_tool_call_arguments(
                                    proposal_tool_name,
                                    cast(dict[str, Any], proposal_summary_arguments),
                                    telemetry=ctx.redaction_telemetry,
                                )
                    except ToolArgumentError:
                        # Preserve the established explicit-approval contract for
                        # ordinary incremental proposals: semantic tool-argument
                        # rejection occurs only if the user approves execution.
                        pass
                    except BaseException as exc:
                        preproposal_exception = exc
                        proposal_redacted_arguments = None

                if proposal_redacted_arguments is not None and proposal_tool_name == tool_name:
                    proposal_arguments = arguments
                    proposal_summary_arguments = arguments

                if proposal_redacted_arguments is not None:
                    proposal_summary = build_tool_proposal_summary(
                        tool_name=proposal_tool_name,
                        arguments=proposal_summary_arguments,
                        redacted_arguments=proposal_redacted_arguments,
                    )
                    if proposal_tool_name == "set_pipeline":
                        proposal_base = (
                            PresentBase(
                                state_id=UUID(current_state_id),
                                composition_content_hash=composition_content_hash(state),
                            )
                            if current_state_id is not None
                            else AbsentBase()
                        )
                        pipeline_proposal = PipelineProposal.create(
                            pipeline=proposal_arguments,
                            base=proposal_base,
                            repair_count=0,
                            skill_hash=ctx.provenance.skill_hash,
                        )
                        if type(ctx.session_operation_context) is not SessionOperationContext:
                            raise AuditIntegrityError("Composition proposal creation requires exact session operation authority")
                        proposal = await _await_started_tool_stage(
                            turn_sessions_service.create_pipeline_composition_proposal(
                                session_id=turn_session_uuid,
                                session_operation_context=ctx.session_operation_context,
                                plan=PipelinePlanResult(
                                    proposal=pipeline_proposal,
                                    tool_call_id=tool_call.id,
                                    custody_result=pipeline_custody_result,
                                    model_identifier=ctx.provenance.model_identifier,
                                    model_version=provider_model_version,
                                    provider=ctx.provenance.provider or "unknown",
                                ),
                                summary=proposal_summary.summary,
                                rationale=proposal_summary.rationale,
                                affects=proposal_summary.affects,
                                arguments_redacted_json=proposal_summary.arguments_redacted_json,
                                actor=f"composer-web:user:{user_id}" if user_id is not None else "composer-web:anonymous",
                                user_message_id=UUID(user_message_id) if user_message_id is not None else None,
                                composer_model_identifier=ctx.provenance.model_identifier,
                                composer_model_version=provider_model_version,
                                composer_provider=ctx.provenance.provider or "unknown",
                            ),
                            audit=audit,
                            recorder=recorder,
                            failure_observation=observation,
                        )
                    else:
                        if type(ctx.session_operation_context) is not SessionOperationContext:
                            raise AuditIntegrityError("Composition proposal creation requires exact session operation authority")
                        proposal = await _await_started_tool_stage(
                            turn_sessions_service.create_composition_proposal(
                                session_id=turn_session_uuid,
                                session_operation_context=ctx.session_operation_context,
                                tool_call_id=tool_call.id,
                                tool_name=proposal_tool_name,
                                summary=proposal_summary.summary,
                                rationale=proposal_summary.rationale,
                                affects=proposal_summary.affects,
                                arguments_json=proposal_arguments,
                                arguments_redacted_json=proposal_summary.arguments_redacted_json,
                                base_state_id=UUID(current_state_id) if current_state_id is not None else None,
                                actor=f"composer-web:user:{user_id}" if user_id is not None else "composer-web:anonymous",
                                user_message_id=UUID(user_message_id) if user_message_id is not None else None,
                                composer_model_identifier=ctx.provenance.model_identifier,
                                composer_model_version=provider_model_version,
                                composer_provider=ctx.provenance.provider or "unknown",
                                composer_skill_hash=ctx.provenance.skill_hash,
                                tool_arguments_hash=audit.binding_arguments_hash,
                            ),
                            audit=audit,
                            recorder=recorder,
                            failure_observation=observation,
                        )
                    proposals_this_turn += 1
                    proposal_result = ToolResult(
                        success=True,
                        updated_state=state,
                        validation=(
                            candidate_prior_validation
                            if candidate_prior_validation is not None
                            else (
                                last_validation
                                if last_validation is not None
                                else ctx.policy_catalog.validate_composition_state(state).validation
                            )
                        ),
                        affected_nodes=(),
                        # Built inline and never bound to a name, so nothing stands between
                        # construction and the freeze in ``ToolResult.__post_init__``: there is no
                        # local, alias, ``cast`` widening or callee that could re-shape it.
                        # No ``success`` inside the payload: the envelope's own ``success`` already
                        # says it, and ``status`` is the discriminator a reader keys on
                        # (elspeth-e405ad7cd2, F1).
                        data=_ProposalPayload(
                            status="APPROVAL_REQUIRED",
                            proposal_id=str(proposal.id),
                            tool_name=proposal_tool_name,
                            summary=proposal.summary,
                            message="The requested pipeline change is pending human approval and has not been applied.",
                        ),
                    )
                    recorder.record(
                        finish_success(
                            audit,
                            result_payload=proposal_result.to_dict(),
                            version_after=state.version,
                        )
                    )
                    _append_tool_outcome(
                        response=proposal_result,
                        error_class=None,
                        error_category=None,
                        error_message=None,
                        post_version=state.version,
                    )
                    anti_anchor.record_success()
                    tool_feedback_json = _serialize_tool_result(proposal_result)
                    llm_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_feedback_json,
                            PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                        }
                    )
                    turn_has_mutation = True
                    continue
                # ``None`` means invalid LLM arguments, an unacceptable
                # prevalidated set_pipeline candidate, or captured one-time
                # pre-proposal work. All fall through to the canonical
                # dispatch/outcome path below.

            await _await_started_tool_stage(
                emit_progress(progress, tool_started_progress_event(tool_name)),
                audit=audit,
                recorder=recorder,
                failure_observation=observation,
            )

            # Advisor escape-hatch interception. The request_advisor_hint
            # tool is intercepted here BEFORE execute_tool() because the
            # action is an async LiteLLM call to a frontier model rather
            # than a sync state mutation. The tool is not registered in
            # _DISCOVERY_TOOLS or _MUTATION_TOOLS, so execute_tool() would
            # return "Unknown tool" if this branch did not handle it.
            #
            # Audit envelope was already opened above by
            # begin_dispatch_or_arg_error; this branch closes it with the
            # truthful dispatch status: ARG_ERROR for local advisor-argument
            # rejection, SUCCESS for completed policy/provider outcomes
            # whose semantic status is encoded in result_payload. The inner
            # LLM call is recorded separately via _call_advisor_with_audit
            # firing a ComposerLLMCall record.
            if tool_name == "request_advisor_hint":
                if resolve_tool_effects(tool_name, arguments).domains:
                    raise AssertionError("Advisor dispatch must not author composition domains.")
                # Successful advisor guidance is governed solely by the
                # advisor budget so the composer can read it. Advisor
                # policy/error feedback with no usable guidance is still
                # a non-mutating correction turn, so it consumes discovery
                # budget before the loop asks the primary model again.
                # F3: validate argument types and total prompt size at the
                # Tier-3 trust boundary. _TOOL_REQUIRED_PATHS only checks
                # key presence, not value shape. Without this check the
                # LLM could send a non-list (silently iterated char-by-
                # char) or a megabyte-scale value (unbounded provider
                # cost). ARG_ERRORs do NOT consume advisor budget — no
                # outbound call is made — but anti-anchor counts them
                # so repeated identical bad-arg calls trigger the §7.7
                # structural hint.
                if type(ctx.advisor_checkpoint) is not AdvisorCheckpointOwner:
                    raise AuditIntegrityError("Manual Advisor admission requires the owned checkpoint owner")
                advisor_arg_error = AdvisorCheckpointOwner._validate_advisor_arguments(ctx.advisor_checkpoint, arguments)
                if not isinstance(advisor_arg_error, RequestAdvisorHintArgumentsModel):
                    advisor_rejection_payload = advisor_arg_error.to_payload()
                    recorder.record(
                        finish_arg_error(
                            audit,
                            error_class=advisor_arg_error.error_class,
                            error_category=advisor_arg_error.category,
                            error_message=advisor_arg_error.error,
                            error_payload=advisor_rejection_payload,
                        )
                    )
                    _append_tool_outcome(
                        response=None,
                        error_class=advisor_arg_error.error_class,
                        error_category=advisor_arg_error.category,
                        error_message=advisor_arg_error.error,
                        post_version=state.version,
                    )
                    anti_anchor.record_failure(tool_name, audit.arguments_hash, category=advisor_arg_error.category)
                    tool_feedback_json = json.dumps(advisor_feedback_payload(advisor_rejection_payload))
                    _append_issued_feedback(tool_feedback_json)
                    turn_has_discovery = True
                    continue

                budget = ctx.advisor_max_calls_per_compose
                if batch.manual_advisor_allowance is None:
                    batch.manual_advisor_allowance = ManualAdvisorAllowance(limit=budget, used=advisor_calls_used)
                allowance = batch.manual_advisor_allowance
                if type(allowance) is not ManualAdvisorAllowance:
                    raise AuditIntegrityError("Manual Advisor admission lost its nominal allowance owner")
                if allowance.limit != budget:
                    raise AuditIntegrityError("Manual Advisor allowance changed during its issued batch")
                advisor_calls_used = allowance.used
                if allowance.available <= 0:
                    budget_payload = {
                        "status": "BUDGET_EXHAUSTED",
                        "budget_used": advisor_calls_used,
                        "budget_remaining": 0,
                        "guidance": (
                            f"Advisor budget exhausted ({advisor_calls_used}/{budget} calls "
                            "used this compose request). Return to the validator output and "
                            "the recovery cheat sheet — no more frontier hints are available "
                            "until the operator raises the budget or the next compose request."
                        ),
                    }
                    recorder.record(
                        finish_success(
                            audit,
                            result_payload=budget_payload,
                            version_after=state.version,
                        )
                    )
                    _append_tool_outcome(
                        response=budget_payload,
                        error_class=None,
                        error_category=None,
                        error_message=None,
                        post_version=state.version,
                    )
                    # Budget exhaustion is a structural signal back to
                    # the LLM, not a tool-failure pattern — do NOT count
                    # it for §7.7 anchor tracking, since the issue isn't
                    # the LLM repeating an identical request, it's the
                    # operator's policy refusing further hints.
                    tool_feedback_json = json.dumps(advisor_feedback_payload(budget_payload))
                    _append_issued_feedback(tool_feedback_json)
                    turn_has_discovery = True
                    continue

                remaining = _remaining_compose_seconds(deadline)
                if remaining <= 0:
                    # Nothing was raised here: the pre-call deadline check refused
                    # the call, so the payload names no exception class. The
                    # ``COMPOSE_TIMEOUT`` status is the discriminant.
                    pre_call_timeout_payload: dict[str, Any] = {
                        "status": "COMPOSE_TIMEOUT",
                        "error": "Advisor call exceeded the remaining compose deadline.",
                        "budget_used": advisor_calls_used,
                        "budget_remaining": budget - advisor_calls_used,
                    }
                    recorder.record(
                        finish_success(
                            audit,
                            result_payload=pre_call_timeout_payload,
                            version_after=state.version,
                        )
                    )
                    _append_tool_outcome(
                        response=pre_call_timeout_payload,
                        error_class=None,
                        error_category=None,
                        error_message=None,
                        post_version=state.version,
                    )
                    advisor_compose_timeout = "pre_call"
                    break

                elif remaining < _MIN_USEFUL_ADVISOR_SECONDS:
                    deadline_payload = {
                        "status": "DEADLINE_TOO_CLOSE",
                        "outbound_call_made": False,
                        "budget_used": advisor_calls_used,
                        "budget_remaining": budget - advisor_calls_used,
                    }
                    recorder.record(
                        finish_success(
                            audit,
                            result_payload=deadline_payload,
                            version_after=state.version,
                        )
                    )
                    _append_tool_outcome(
                        response=deadline_payload,
                        error_class=None,
                        error_category=None,
                        error_message=None,
                        post_version=state.version,
                    )
                    tool_feedback_json = json.dumps(advisor_feedback_payload(deadline_payload))
                    _append_issued_feedback(tool_feedback_json)
                    turn_has_discovery = True
                    continue

                advisor_timeout = ctx.advisor_timeout_seconds
                effective_advisor_timeout = min(advisor_timeout, remaining)
                advisor_deadline_limited = remaining <= advisor_timeout

                # Reserve before awaiting preparation. Commit at the physical
                # dispatch callback, irrespective of provider result or later
                # audit-buffer failure. Release every undispatched exit before
                # the outer timeout/failure classification reads the allowance.
                reservation = allowance.reserve()
                if reservation is None:
                    raise AuditIntegrityError("Manual Advisor admission lost its reserved allowance")

                abandonment_context = None
                try:
                    try:
                        if ctx.turn_evidence is not None:
                            ctx.turn_evidence.transcript.observe_messages(llm_messages)
                        if advisor_arg_error.trigger == ADVISOR_TRIGGER_CANNOT_COMPLETE:
                            if ctx.turn_evidence is None or user_message_content is None:
                                raise AuditIntegrityError("Abandonment review requires current request-owned evidence")
                            abandonment_context = ctx.turn_evidence.abandonment_context(
                                request=user_message_content,
                                current_state=state,
                                plugin_snapshot=ctx.plugin_snapshot,
                                messages=llm_messages,
                            )
                        advisor_outcome = await _await_started_tool_stage(
                            ctx.advisor_checkpoint._call_advisor_for_tool(
                                advisor_arg_error,
                                recorder=recorder,
                                timeout=effective_advisor_timeout,
                                provider_owner=ctx.provider_owner,
                                abandonment_context=abandonment_context,
                                advisor_transcript=ctx.turn_evidence.transcript.snapshot() if ctx.turn_evidence is not None else None,
                                current_state=state,
                                session_id=ctx.session_id,
                                session_operation_context=ctx.session_operation_context,
                                on_provider_dispatch=reservation.dispatch,
                                failure_observation=observation,
                            ),
                            audit=audit,
                            recorder=recorder,
                            failure_observation=observation,
                        )
                    finally:
                        reservation.close()
                        advisor_calls_used = allowance.used
                except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                    # Lifecycle exceptions: do not absorb. Propagate so
                    # cancellation and shutdown work normally; the inner
                    # ComposerLLMCall record was already fired by the
                    # advisor method's finally block, so the audit trail
                    # captures the cancelled call regardless.
                    raise
                except TimeoutError as advisor_exc:
                    if advisor_deadline_limited:
                        timeout_payload = {
                            "status": "COMPOSE_TIMEOUT",
                            "error": "Advisor call exceeded the remaining compose deadline.",
                            "error_class": type(advisor_exc).__name__,
                            "budget_used": advisor_calls_used,
                            "budget_remaining": budget - advisor_calls_used,
                        }
                        recorder.record(
                            finish_success(
                                audit,
                                result_payload=timeout_payload,
                                version_after=state.version,
                            )
                        )
                        _append_tool_outcome(
                            response=timeout_payload,
                            error_class=None,
                            error_category=None,
                            error_message=None,
                            post_version=state.version,
                        )
                        advisor_compose_timeout = "in_flight"
                        if failure_custody is not None:
                            failure_custody.consume_nonprimary()
                        break
                    # Advisor-specific timeout with compose budget still
                    # remaining: return structured tool feedback so the
                    # composer can continue within its global deadline.
                    advisor_error_payload = {
                        "status": "ADVISOR_ERROR",
                        "error": "Advisor call failed; no guidance returned.",
                        "error_class": type(advisor_exc).__name__,
                        "budget_used": advisor_calls_used,
                        "budget_remaining": budget - advisor_calls_used,
                    }
                    recorder.record(
                        finish_success(
                            audit,
                            result_payload=advisor_error_payload,
                            version_after=state.version,
                        )
                    )
                    _append_tool_outcome(
                        response=advisor_error_payload,
                        error_class=None,
                        error_category=None,
                        error_message=None,
                        post_version=state.version,
                    )
                    anti_anchor.record_failure(tool_name, audit.arguments_hash)
                    tool_feedback_json = json.dumps(advisor_feedback_payload(advisor_error_payload))
                    _append_issued_feedback(tool_feedback_json)
                    turn_has_discovery = True
                    if failure_custody is not None:
                        failure_custody.consume_nonprimary()
                    continue

                except Exception as advisor_context_error:
                    advisor_outcome = _AdvisorFirstPartyFailure(original_exc=advisor_context_error)

                if type(advisor_outcome) is _AdvisorProviderFailure:
                    # The service owns the Tier-3 provider taxonomy and returns
                    # this explicit recoverable outcome. Only an actual
                    # dispatch spends the reservation, regardless of guidance.
                    # Advisor failure IS counted for §7.7 anchor
                    # tracking — repeated identical failed advisor
                    # calls indicate the LLM is spamming a broken
                    # prompt, exactly the pattern the tracker exists
                    # to break.
                    if advisor_outcome.provider_failure is None:
                        advisor_error_payload = {
                            "status": "ADVISOR_ERROR",
                            "error": "Advisor call failed; no guidance returned.",
                            "error_class": advisor_outcome.error_class,
                            "budget_used": advisor_calls_used,
                            "budget_remaining": budget - advisor_calls_used,
                        }
                        recorder.record(
                            finish_success(
                                audit,
                                result_payload=advisor_error_payload,
                                version_after=state.version,
                            )
                        )
                        _append_tool_outcome(
                            response=advisor_error_payload,
                            error_class=None,
                            error_category=None,
                            error_message=None,
                            post_version=state.version,
                        )
                        anti_anchor.record_failure(tool_name, audit.arguments_hash)
                        tool_feedback_json = json.dumps(advisor_feedback_payload(advisor_error_payload))
                    else:
                        request_rejection_payload = {
                            "status": "ADVISOR_ERROR",
                            "error": advisor_outcome.provider_failure.details.explanation,
                            "error_class": advisor_outcome.error_class,
                            "budget_used": advisor_calls_used,
                            "budget_remaining": budget - advisor_calls_used,
                            "request_rejection": advisor_outcome.provider_failure.details.to_wire(),
                        }
                        recorder.record(
                            finish_success(
                                audit,
                                result_payload=request_rejection_payload,
                                version_after=state.version,
                            )
                        )
                        _append_tool_outcome(
                            response=request_rejection_payload,
                            error_class=None,
                            error_category=None,
                            error_message=None,
                            post_version=state.version,
                        )
                        anti_anchor.record_failure(tool_name, audit.arguments_hash)
                        tool_feedback_json = json.dumps(advisor_feedback_payload(request_rejection_payload))
                    _append_issued_feedback(tool_feedback_json)
                    turn_has_discovery = True
                    if failure_custody is not None:
                        failure_custody.consume_nonprimary()
                    continue

                if type(advisor_outcome) is _AdvisorFirstPartyFailure:
                    # A controlled-code fault is not provider feedback. Close the
                    # outer dispatch truthfully, carry it through P4, and let the
                    # driver re-raise this exact exception object after the tool
                    # row has been published.
                    first_party_exc = advisor_outcome.original_exc
                    if failure_custody is not None:
                        failure_custody.observe_primary(first_party_exc)
                        if failure_custody.errors or isinstance(first_party_exc, BaseExceptionGroup):
                            raise failure_custody.failure(first_party_exc)
                    if isinstance(first_party_exc, _ServiceFailure):
                        _complete_service_failure(first_party_exc)
                        break
                    if isinstance(first_party_exc, ComposerContextUnsupported):
                        context_payload = {
                            "success": False,
                            "outbound_call_made": False,
                            "error_type": "composer_context_unsupported",
                            "failure_code": "context_unsupported",
                            "reason": first_party_exc.reason,
                            "detail": first_party_exc.to_payload()["detail"],
                            "budget_used": advisor_calls_used,
                            "budget_remaining": budget - advisor_calls_used,
                        }
                        context_invocation = finish_success(audit, result_payload=context_payload, version_after=state.version)
                        if observation is None:
                            recorder.record(context_invocation)
                        else:
                            recorder.record_with_failure_observation(context_invocation, observation)
                        _append_tool_outcome(
                            response=context_payload,
                            error_class=None,
                            error_category=None,
                            error_message=None,
                            post_version=state.version,
                        )
                        advisor_failure = first_party_exc
                        if failure_custody is not None:
                            failure_custody.promote_completed_failure(
                                kind="advisor_failure", original=first_party_exc, refusal="context_unsupported", allowance_limit=budget
                            )
                        break
                    if isinstance(first_party_exc, CommonProviderCostRefused):
                        cost_refusal_payload = {
                            "success": False,
                            "status": "PROVIDER_POLICY_REFUSED",
                            "code": first_party_exc.code,
                            "outbound_call_made": first_party_exc.call_id is not None,
                        }
                        cost_invocation = finish_success(audit, result_payload=cost_refusal_payload, version_after=state.version)
                        if observation is None:
                            recorder.record(cost_invocation)
                        else:
                            recorder.record_with_failure_observation(cost_invocation, observation)
                        _append_tool_outcome(
                            response=cost_refusal_payload,
                            error_class=None,
                            error_category=None,
                            error_message=None,
                            post_version=state.version,
                        )
                        advisor_failure = first_party_exc
                        if failure_custody is not None:
                            failure_custody.promote_completed_failure(
                                kind="advisor_failure",
                                original=first_party_exc,
                                refusal="provider_cost_refused",
                                provider_call_id=first_party_exc.call_id,
                            )
                        break
                    if isinstance(first_party_exc, PromptGuardRefused):
                        # The tool completed with a semantic refusal. It is neither
                        # provider-outage feedback nor a plugin implementation fault.
                        refusal_payload = {"success": False, "outbound_call_made": False, **first_party_exc.to_payload()}
                        guard_invocation = finish_success(audit, result_payload=refusal_payload, version_after=state.version)
                        if observation is None:
                            recorder.record(guard_invocation)
                        else:
                            recorder.record_with_failure_observation(guard_invocation, observation)
                        _append_tool_outcome(
                            response=refusal_payload,
                            error_class=None,
                            error_category=None,
                            error_message=None,
                            post_version=state.version,
                        )
                        advisor_failure = first_party_exc
                        if failure_custody is not None:
                            failure_custody.promote_completed_failure(
                                kind="advisor_failure", original=first_party_exc, refusal="prompt_guard_refused"
                            )
                        break
                    advisor_invocation = finish_plugin_crash(audit, exc=first_party_exc)
                    if observation is None:
                        recorder.record(advisor_invocation)
                    else:
                        recorder.record_with_failure_observation(advisor_invocation, observation)
                    _append_tool_outcome(
                        response=None,
                        error_class=type(first_party_exc).__name__,
                        error_category=None,
                        error_message=type(first_party_exc).__name__,
                        post_version=state.version,
                    )
                    advisor_failure = first_party_exc
                    if failure_custody is not None:
                        failure_custody.promote_completed_failure(kind="advisor_failure", original=first_party_exc)
                    break

                if type(advisor_outcome) is not _AdvisorCallSuccess:
                    raise AuditIntegrityError("Advisor call returned an unknown owned outcome")
                guidance = advisor_outcome.guidance
                advisor_meta = advisor_outcome.metadata

                abandonment_decision = None
                review_binding = None
                advisor_note = (
                    "ADVICE only — call the appropriate mutation tool to "
                    "apply any change. Do not echo this guidance back as "
                    "configuration without verifying it against the schema."
                )
                if advisor_outcome.abandonment_response is not None:
                    if abandonment_context is None or ctx.turn_evidence is None:
                        raise AuditIntegrityError("Abandonment decision lost its request-owned context")
                    decision = advisor_outcome.abandonment_response
                    ctx.turn_evidence.accept_abandonment(
                        abandonment_context, decision, current_state=state, plugin_snapshot=ctx.plugin_snapshot
                    )
                    abandonment_decision = decision.decision
                    review_binding = abandonment_context.binding
                    guidance = decision.reason
                    if decision.next_action is not None:
                        guidance += "\nSupported next action: " + decision.next_action
                    if decision.question is not None:
                        guidance += "\nQuestion for the user: " + decision.question
                    if decision.decision == "STOP_CONFIRMED":
                        advisor_note = (
                            "Stop authoring this request: no remaining authorized useful action can proceed. Explain the confirmed limit "
                            "or ask the precise question; preserve the draft and do not claim completion. The final reply still receives ordinary END review."
                        )
                    elif decision.decision == "CONTINUE_WITH_LIMITATION":
                        advisor_note = (
                            "Continue the supported work with the advisor's next action. Retain the precise limitation for final review; "
                            "it does not stop the whole request or establish pipeline completion or graph clearance."
                        )
                    else:
                        advisor_note = "Continue with the advisor's supported action. This is not pipeline completion or graph clearance."
                success_payload = {
                    "status": "SUCCESS",
                    "guidance": guidance,
                    "model": advisor_meta["model"],
                    "prompt_tokens": advisor_meta["prompt_tokens"],
                    "completion_tokens": advisor_meta["completion_tokens"],
                    "cached_prompt_tokens": advisor_meta["cached_prompt_tokens"],
                    "advisor_latency_ms": advisor_meta["latency_ms"],
                    "budget_used": advisor_calls_used,
                    "budget_remaining": budget - advisor_calls_used,
                    "note": advisor_note,
                    "abandonment_decision": abandonment_decision,
                    "review_binding": review_binding,
                }
                recorder.record(
                    finish_success(
                        audit,
                        result_payload=success_payload,
                        version_after=state.version,
                    )
                )
                _append_tool_outcome(
                    response=success_payload,
                    error_class=None,
                    error_category=None,
                    error_message=None,
                    post_version=state.version,
                )
                # Successful advisor call is progress, not a failure —
                # the §7.7 tracker is reset for this tool name so a
                # subsequent set_pipeline failure does not inherit a
                # stale advisor anchor count.
                anti_anchor.record_success()
                tool_feedback_json = json.dumps(advisor_feedback_payload(success_payload))
                feedback_message = {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": tool_feedback_json,
                    PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                }
                if abandonment_context is not None and ctx.turn_evidence is not None:
                    ctx.turn_evidence.seal_abandonment_feedback(abandonment_context, feedback_message)
                if publication is not None:
                    publication.register_feedback(serialized=tool_feedback_json, message=feedback_message)
                llm_messages.append(feedback_message)
                continue

            # Session-aware async tool dispatch.
            #
            # ``_SESSION_AWARE_TOOL_HANDLERS`` holds handlers that AWAIT
            # session-service writers/readers; they cannot be dispatched
            # through the sync ``run_sync_in_worker(execute_tool, ...)``
            # path because execute_tool() is sync. The compose loop
            # intercepts these tool names BEFORE the generic dispatch
            # and awaits the handler directly, then routes the result
            # through the same audit envelope discipline as the sync
            # path (finish_success / finish_arg_error / finish_plugin_crash).
            #
            # Currently registered: ``request_interpretation_review``.
            # Adding another session-aware tool extends
            # ``_SESSION_AWARE_TOOL_HANDLERS`` and the per-tool
            # kwarg-build dict below; no new dispatch branch is needed.
            if is_session_aware_tool(tool_name):
                # Preflight is infrastructure work, before the session-aware
                # handler. Its failure/deadline carriers must reach the phase
                # owner without being recategorized as a handler crash.
                try:
                    review_preflight = await _await_started_tool_stage(
                        _pending_review_runtime_findings(state, ctx), audit=audit, recorder=recorder, failure_observation=observation
                    )
                except ComposerRuntimePreflightError as preflight_exc:
                    raise ComposerRuntimePreflightError(
                        original_exc=preflight_exc.original_exc,
                        partial_state=preflight_exc.partial_state,
                        tool_invocations=recorder.invocations,
                        llm_calls=recorder.llm_calls,
                        failed_turn=preflight_exc.failed_turn,
                    ) from preflight_exc.original_exc
                handler_failure: _ServiceHandlerFailure | None = None
                try:
                    session_aware_outcome = await _await_started_tool_stage(
                        ctx.session_tools._dispatch_session_aware_tool(
                            tool_name=tool_name,
                            tool_call_id=tool_call.id,
                            arguments=arguments,
                            state=state,
                            audit=audit,
                            recorder=recorder,
                            session_id=session_id,
                            session_operation_context=ctx.session_operation_context,
                            current_state_id=current_state_id,
                            composer_model_version=provider_model_version,
                            llm_messages=llm_messages,
                            anti_anchor=anti_anchor,
                            policy_catalog=ctx.policy_catalog,
                            review_preflight=review_preflight,
                            failure_observation=observation,
                            publication=publication,
                        ),
                        audit=audit,
                        recorder=recorder,
                        failure_observation=observation,
                    )
                except _ServiceHandlerFailure as marker:
                    handler_failure = marker
                except (AssertionError, MemoryError, RecursionError, SystemError):
                    # Same narrow-class discipline as the sync execute_tool
                    # handler below: interpreter/Tier-1 invariant states must
                    # unwind, never be laundered into a recoverable envelope.
                    raise
                except AuditIntegrityError:
                    # Tier-1 audit invariant — same passthrough as the sync path.
                    raise
                except Exception as tool_exc:
                    if isinstance(tool_exc, _ServiceFailure):
                        # Only the inner handler's owned marker can close a
                        # service refusal. Result/audit/post-handler failures
                        # retain their real owner without inventing completion.
                        raise
                    # elspeth-9c01c943a5: the session-aware dispatch converts
                    # ToolArgumentError internally, so any exception reaching
                    # here is a plugin/service bug. Without this capture it
                    # escaped run_tool_batch and surfaced as an uncoded 500
                    # with the accumulated turn state silently dropped. Route
                    # it through the same plugin-crash carrier as the sync
                    # path so the turn persists and the route layer answers
                    # with the coded envelope.
                    if failure_custody is not None:
                        failure_custody.observe_primary(tool_exc)
                        if failure_custody.errors or observation is None or not observation.execution_failed:
                            combined = failure_custody.failure(tool_exc)
                            if combined is tool_exc:
                                raise
                            raise combined from combined.__cause__
                        recorded_crash = finish_plugin_crash(audit, exc=tool_exc)
                        recorder.record_with_failure_observation(recorded_crash, observation)
                    else:
                        recorder.record(finish_plugin_crash(audit, exc=tool_exc))
                    _append_tool_outcome(
                        response=None,
                        error_class=type(tool_exc).__name__,
                        error_category=None,
                        error_message=type(tool_exc).__name__,
                        post_version=state.version,
                    )
                    plugin_crash = ComposerPluginCrashError.capture(
                        tool_exc,
                        state=state,
                        initial_version=initial_version,
                        tool_invocations=recorder.invocations,
                        llm_calls=recorder.llm_calls,
                    )
                    plugin_crash_cause = tool_exc
                    if failure_custody is not None:
                        failure_custody.promote_completed_failure(kind="plugin_crash", original=tool_exc, wrapper=plugin_crash)
                    break
                if handler_failure is not None:
                    if (
                        type(handler_failure) is not _ServiceHandlerFailure
                        or handler_failure.audit is not audit
                        or handler_failure.tool_call_id != tool_call.id
                    ):
                        raise AuditIntegrityError(
                            "service handler failure changed its actual issued audit/call"
                        ) from handler_failure.original
                    # Leave the marker's exception suite before audit closure.
                    # A real secondary must keep its own context, never acquire
                    # this private routing marker as an implicit exception link.
                    _complete_service_failure(handler_failure.original)
                    break
                all_cache_hits = False
                _append_tool_outcome(
                    response=session_aware_outcome.result,
                    error_class=session_aware_outcome.error_class,
                    error_category=session_aware_outcome.error_category,
                    error_message=session_aware_outcome.error_message,
                    post_version=session_aware_outcome.post_version,
                )
                if publication is not None and session_aware_outcome.result is None:
                    publication.bind_completed_outcome(tool_outcomes[-1])
                if session_aware_outcome.is_discovery:
                    turn_has_discovery = True
                else:
                    turn_has_mutation = True
                if session_aware_outcome.result is not None:
                    state = session_aware_outcome.result.updated_state
                    last_validation = session_aware_outcome.result.validation
                continue

            # Precompute runtime preflight for preview_pipeline outside
            # the general side-effectful tool worker. This keeps
            # execute_tool() synchronous and bounds the async I/O cost
            # before it enters the worker thread pool.
            runtime_preflight_callback: RuntimePreflight | None = None
            structural_preflight_callback: RuntimePreflight | None = None
            preview_claims_admitted = True
            if tool_name == "preview_pipeline":
                try:
                    _admit_source_proof_claims(state)
                except MalformedSourceProofClaim:
                    # The ordinary producer owns the saved-graph refusal. Do not
                    # inspect any resource before its complete scalar admission.
                    preview_claims_admitted = False
            if tool_name == "preview_pipeline" and preview_claims_admitted:
                try:
                    preview_preflight = await ctx.preflight.cached_runtime_preflight(
                        state,
                        user_id=user_id,
                        session_id=session_id,
                        cache=runtime_preflight_cache,
                        initial_version=initial_version,
                        session_scope=session_scope,
                        llm_calls=recorder.llm_calls,
                        plugin_snapshot=ctx.plugin_snapshot,
                        session_operation_context=ctx.session_operation_context,
                    )
                except asyncio.CancelledError as cancellation:
                    if observation is not None:
                        observation.observe_primary(cancellation)
                        recorder.record_with_failure_observation(finish_cancelled(audit, exc=cancellation), observation)
                    else:
                        recorder.record(finish_cancelled(audit, exc=cancellation))
                    raise
                except ComposerRuntimePreflightError as preflight_exc:
                    if observation is not None:
                        observation.observe_primary(preflight_exc.original_exc)
                        recorder.record_with_failure_observation(finish_plugin_crash(audit, exc=preflight_exc.original_exc), observation)
                    else:
                        recorder.record(finish_plugin_crash(audit, exc=preflight_exc.original_exc))
                    raise ComposerRuntimePreflightError.capture(
                        preflight_exc.original_exc,
                        state=state,
                        initial_version=initial_version,
                        tool_invocations=recorder.invocations,
                        llm_calls=recorder.llm_calls,
                    ) from preflight_exc.original_exc

                def _make_preflight_callback(
                    _result: ValidationResult = preview_preflight,
                ) -> RuntimePreflight:
                    def _callback(_state: CompositionState) -> ValidationResult:
                        return _result

                    return _callback

                runtime_preflight_callback = _make_preflight_callback()

                # elspeth-229e9e8195: while an interpretation review is pending
                # the strict verdict above is handoff-shaped and every stage past
                # ``review_interpretations`` is SKIPPED_AFTER_FAILURE — so also
                # precompute the interpretation-tolerant preflight and let the
                # preview surface its structural findings during authoring
                # instead of first at completion. Same cache and snapshot as the
                # completion path's ``_pending_handoff_outstanding_findings``, so
                # the two tolerant lookups share one engine run per state. A
                # tolerant-pass infrastructure failure propagates exactly like
                # the strict one (the completion path takes the same position):
                # an explicit failure beats silently withholding findings this
                # turn claimed to check for.
                if is_pending_interpretation_handoff(preview_preflight):
                    try:
                        structural_preflight_result = await _await_started_tool_stage(
                            ctx.preflight.cached_runtime_preflight(
                                state,
                                user_id=user_id,
                                session_id=session_id,
                                cache=runtime_preflight_cache,
                                initial_version=initial_version,
                                session_scope=session_scope,
                                llm_calls=recorder.llm_calls,
                                plugin_snapshot=ctx.plugin_snapshot,
                                session_operation_context=ctx.session_operation_context,
                                interpretation_tolerant=True,
                            ),
                            audit=audit,
                            recorder=recorder,
                            failure_observation=observation,
                        )
                    except ComposerRuntimePreflightError as preflight_exc:
                        if observation is not None:
                            observation.observe_primary(preflight_exc.original_exc)
                            recorder.record_with_failure_observation(
                                finish_plugin_crash(audit, exc=preflight_exc.original_exc), observation
                            )
                        else:
                            recorder.record(finish_plugin_crash(audit, exc=preflight_exc.original_exc))
                        raise ComposerRuntimePreflightError.capture(
                            preflight_exc.original_exc,
                            state=state,
                            initial_version=initial_version,
                            tool_invocations=recorder.invocations,
                            llm_calls=recorder.llm_calls,
                        ) from preflight_exc.original_exc

                    def _make_structural_callback(
                        _result: ValidationResult = structural_preflight_result,
                    ) -> RuntimePreflight:
                        def _callback(_state: CompositionState) -> ValidationResult:
                            return _result

                        return _callback

                    structural_preflight_callback = _make_structural_callback()

            # All tool calls are offloaded to a worker to avoid blocking
            # the event loop.
            # Blob and secret tools perform synchronous filesystem
            # writes and SQLAlchemy transactions that would otherwise
            # stall the single-process web server for all concurrent
            # requests (rate-limit checks, websocket heartbeats,
            # progress broadcasts).
            #
            # Cancel-safety: tool calls are NOT wrapped in
            # asyncio.wait_for — they always run to completion.
            # The cooperative deadline is checked BETWEEN operations
            # (before LLM calls, after tool batches), so side effects
            # and state publication are never split.  LLM calls use
            # per-call wait_for because they are pure network I/O
            # with no side effects.
            #
            # Tool handlers raise ToolArgumentError at Tier-3 boundaries
            # (LLM supplied wrong types, semantically invalid values,
            # or malformed encodings that cannot be coerced).  The
            # compose loop catches ONLY that class and feeds the error
            # back to the LLM for retry.
            #
            # Any other exception — TypeError, ValueError, UnicodeError,
            # KeyError, AttributeError — escaping execute_tool() is a
            # plugin bug (Tier 1/2) and MUST crash.  Per
            # docs/guides/data-trust-and-error-handling.md §Plugin
            # Ownership: System Code, Not User Code, silently
            # laundering a plugin bug as an LLM-argument error
            # is worse than crashing: it pollutes the audit trail with
            # a confident but wrong Tier-3 story, and the LLM's "retry"
            # cannot correct a fault in our own code.
            # Dispatch under the structural audit envelope. The helper
            # records exactly one ComposerToolInvocation before this
            # await returns or raises, on every path:
            #   SUCCESS         → finish_success (with sentinel-canonical
            #                     fallback if canonical_json raises)
            #   ARG_ERROR       → finish_arg_error (caller's except block
            #                     below builds the LLM message)
            #   PLUGIN_CRASH (narrow re-raise) → finish_plugin_crash, then
            #                     re-raised so the outer compose loop exits
            #   PLUGIN_CRASH (general) → finish_plugin_crash, then the
            #                     caller's except block below wraps with
            #                     ComposerPluginCrashError.capture()
            #
            # Closes panel-review blockers B1 (canonical_json failure on
            # success path silently bypassed audit) and B2 (narrow
            # re-raise exited the loop unrecorded).
            # Bind loop-locals as default arguments so the closures
            # capture this iteration's values. The compose loop
            # awaits the helper synchronously inside the same
            # iteration, so late binding would not actually misfire
            # — but `B023 do not let function definitions reference
            # late-bound loop variables` is the project's structural
            # safeguard against future refactors that batch
            # iterations or introduce concurrency. Same pattern the
            # adjacent `_make_preflight_callback` already uses for
            # ``preview_preflight``.
            async def _do_dispatch(
                _tool_name: str = tool_name,
                _arguments: dict[str, Any] = arguments,
                _state: CompositionState = state,
                _last_validation: ValidationSummary | None = last_validation,
                _runtime_preflight_callback: RuntimePreflight | None = runtime_preflight_callback,
                _structural_preflight_callback: RuntimePreflight | None = structural_preflight_callback,
                _user_message_id: str | None = user_message_id,
                _user_message_content: str | None = user_message_content,
                _composer_model_identifier: str = ctx.provenance.model_identifier,
                _composer_model_version: str = provider_model_version,
                _composer_provider: str = ctx.provenance.provider or "unknown",
                _composer_skill_hash: str = ctx.provenance.skill_hash,
                _tool_arguments_hash: str = audit.binding_arguments_hash,
                _interpretation_requirements_are_internal: bool = interpretation_requirements_are_internal,
                _prevalidated_unapplied_result: ToolResult | None = prevalidated_unapplied_result,
                _preproposal_exception: BaseException | None = preproposal_exception,
                _auto_mutation: ComposerMutationCeiling | None = auto_mutation,
                _retained_attachment: AttachmentResultCheckpoint | None = retained_attachment,
            ) -> Any:
                if _preproposal_exception is not None:
                    raise _preproposal_exception
                if _prevalidated_unapplied_result is not None:
                    return _prevalidated_unapplied_result
                if _retained_attachment is not None:
                    return admit_discovery_result(_tool_name, _retained_attachment.result)
                try:
                    dispatched_result = await run_sync_in_worker(
                        execute_tool,
                        _tool_name,
                        _arguments,
                        _state,
                        ctx.policy_catalog,
                        plugin_snapshot=ctx.plugin_snapshot,
                        data_dir=ctx.custody.data_dir,
                        session_engine=ctx.custody.session_engine,
                        session_id=session_id,
                        session_operation_context=ctx.session_operation_context,
                        session_operation_authority=ctx.session_operation_authority,
                        secret_service=ctx.custody.secret_service,
                        secret_wiring_policy=ctx.custody.secret_wiring_policy,
                        user_id=user_id,
                        prior_validation=_last_validation,
                        runtime_preflight=_runtime_preflight_callback,
                        structural_preflight=_structural_preflight_callback,
                        max_blob_storage_per_session_bytes=ctx.custody.max_blob_storage_per_session_bytes,
                        user_message_id=_user_message_id,
                        user_message_content=_user_message_content,
                        composer_model_identifier=_composer_model_identifier,
                        composer_model_version=_composer_model_version,
                        composer_provider=_composer_provider,
                        composer_skill_hash=_composer_skill_hash,
                        tool_arguments_hash=_tool_arguments_hash,
                        mutation_ceiling=_auto_mutation,
                        validate_arguments=True,
                        require_data_dir_for_paths=True,
                        raise_schema_argument_errors=True,
                        _interpretation_requirements_are_internal=_interpretation_requirements_are_internal,
                    )
                except ComposerAutoMutationRefused as exc:
                    acc.authority_revocation = exc.revocation
                    return _failure_result(_state, str(exc), error_code="auto_mutation_revoked", with_state_validation=False)
                if (
                    _tool_name != "set_pipeline"
                    and is_mutation_tool(_tool_name)
                    and dispatched_result.success
                    and dispatched_result.updated_state.version > _state.version
                    and dispatched_result.validation.is_valid
                ):
                    dispatched_result = await _finalize_completed_incremental_mutation(
                        dispatched_result,
                        _state,
                        plugin_snapshot=ctx.plugin_snapshot,
                        policy_catalog=ctx.policy_catalog,
                    )
                if is_discovery_tool(_tool_name) and response_contract_for(_tool_name) is not None:
                    return admit_discovery_result(_tool_name, dispatched_result)
                return dispatched_result

            # ``_arg_error_payload`` is a module-level helper (F2 — testable
            # without spinning up the full compose loop). The nested
            # factory below binds ``tool_name`` to match the dispatch
            # ``arg_error_payload_factory`` signature. Default-arg binding
            # captures the loop-local ``tool_name`` at definition time.
            def _arg_error_payload_factory(
                _exc: ToolArgumentError,
                _tool_name: str = tool_name,
            ) -> Mapping[str, Any]:
                return _arg_error_payload(_exc, _tool_name)

            def _version_after(
                _result: Any,
                _prevalidated_unapplied_result: ToolResult | None = prevalidated_unapplied_result,
                _state: CompositionState = state,
            ) -> int:
                # The handler's ToolResult carries the new state on
                # ``updated_state``. Read here so the helper records
                # the post-mutation version inside the recorder call.
                if _prevalidated_unapplied_result is not None:
                    return _state.version
                if isinstance(_result, AdmittedDiscoveryResult):
                    return _result.result.updated_state.version
                return cast(int, _result.updated_state.version)

            try:
                outcome = await dispatch_with_audit(
                    recorder=recorder,
                    audit=audit,
                    do_dispatch=_do_dispatch,
                    version_after_provider=_version_after,
                    arg_error_payload_factory=_arg_error_payload_factory,
                    failure_observation=observation,
                )
            except ToolArgumentError as exc:
                # The audit record was already written by the helper.
                # Build the LLM-facing tool message and continue.
                if is_discovery_tool(tool_name):
                    turn_has_discovery = True
                else:
                    turn_has_mutation = True
                # Trust-boundary redaction: the echoed message reaches the
                # LLM API and (via audit) the Landscape. ToolArgumentError
                # is structurally safe by construction — the keyword-only
                # constructor accepts (argument, expected, actual_type)
                # and composes args[0] from those fields alone, so the
                # message cannot carry a raw LLM-supplied value. Belt-
                # and-suspenders: read ``exc.args[0]`` rather than
                # ``str(exc)`` so a future subclass that overrides
                # ``__str__`` to embed ``__cause__`` context (which may
                # carry DB URLs, filesystem paths, or secret fragments
                # from deeper layers) cannot leak through this path.
                # Handlers that use
                # ``raise ToolArgumentError(...) from exc`` get the
                # cause preserved on ``__cause__`` for debug/audit but
                # NOT echoed to the LLM.
                await emit_progress(
                    progress,
                    ComposerProgressEvent(
                        phase="using_tools",
                        headline="A tool request needed correction.",
                        evidence=("The tool rejected the request shape without exposing raw values.",),
                        likely_next="ELSPETH will ask the model to adjust the visible tool request.",
                    ),
                )
                arg_error_payload = _arg_error_payload(exc, tool_name)
                _append_tool_outcome(
                    response=None,
                    error_class=type(exc).__name__,
                    error_category=exc.category,
                    error_message=str(exc.args[0] if exc.args else "ToolArgumentError"),
                    post_version=state.version,
                )
                anti_anchor.record_failure(tool_name, audit.arguments_hash, category=exc.category)
                tool_feedback_json = json.dumps(arg_error_payload)
                _append_issued_feedback(tool_feedback_json)
                continue
            except (AssertionError, MemoryError, RecursionError, SystemError):
                # Plugin-ownership policy exception — DOCUMENTED DIVERGENCE.
                #
                # docs/guides/data-trust-and-error-handling.md §Plugin
                # Ownership: System Code, Not User Code says a defective
                # plugin MUST crash rather than be wrapped and laundered
                # as a recoverable error.  The web server relaxes this for
                # ordinary exception classes (see the wider except
                # Exception below) because crashing the whole ASGI
                # process on one bad request would take down every
                # other concurrent session.
                #
                # The exceptions listed on this handler are NOT
                # relaxed: they represent states where the interpreter
                # or our own Tier-1 invariants are compromised and any
                # subsequent work — including the partial-state
                # persistence inside ``ComposerPluginCrashError.capture``
                # — would be operating on potentially-poisoned memory
                # or data.
                #
                # - AssertionError: a plain ``assert`` fired inside
                #   plugin code.  Asserts encode Tier-1 invariants
                #   (crash on any anomaly — see
                #   docs/guides/data-trust-and-error-handling.md §The
                #   Three-Tier Trust Model).  Writing the
                #   composition_states row after an invariant failure
                #   would persist data the invariant said was
                #   impossible.
                # - MemoryError / RecursionError: interpreter-level
                #   resource exhaustion.  The subsequent DB write may
                #   itself fail or corrupt state; better to unwind.
                # - SystemError: CPython internal invariant breach.
                #
                # ``BaseException``-only classes (SystemExit,
                # KeyboardInterrupt, GeneratorExit) already propagate
                # through ``except Exception`` below without any
                # handling here.
                #
                # Audit-record-then-raise discipline: ``dispatch_with_audit``
                # writes a ``PLUGIN_CRASH`` invocation BEFORE the
                # narrow-class exception leaves the helper. Building the
                # invocation reads only pre-captured scalars from the
                # frozen :class:`DispatchAudit` envelope plus
                # ``type(exc).__name__`` — no poisoned memory work.
                # Closes blocker B2 from the panel review (2026-05-04).
                raise
            except ComposerRuntimePreflightError as preflight_exc:
                # A successful mutation may precede its structural preflight.
                # Preserve that new state instead of recapturing the old loop state.
                raise ComposerRuntimePreflightError(
                    original_exc=preflight_exc.original_exc,
                    partial_state=preflight_exc.partial_state,
                    tool_invocations=recorder.invocations,
                    llm_calls=recorder.llm_calls,
                    failed_turn=preflight_exc.failed_turn,
                ) from preflight_exc.original_exc
            except AuditIntegrityError:
                # Tier-1 audit invariant. Do not let the plugin-bug
                # catch-all below launder it into ComposerPluginCrashError.
                raise
            except Exception as tool_exc:
                # Plugin-bug path: any exception class OTHER than
                # ToolArgumentError escaping execute_tool() is a plugin
                # bug (docs/guides/data-trust-and-error-handling.md
                # §Plugin Ownership: System Code, Not User Code).
                # Capture the loop-local ``state`` — which has been
                # rebound to result.updated_state on every successful
                # prior iteration — so the route layer can persist the
                # accumulated mutations into composition_states before
                # returning the 500. Without this, any tool call that
                # successfully mutated state prior to the crash would
                # be silently dropped from the state history.
                #
                # Web-server policy exception: the plugin-ownership rule
                # (docs/guides/data-trust-and-error-handling.md §Plugin
                # Ownership: System Code, Not User Code) says a
                # defective plugin must crash.  In the pipeline engine
                # (single-shot CLI process) that is straightforward —
                # abort the run.  In the web server a single malformed
                # request reaching a buggy tool handler would take the
                # ASGI worker down and abort every other concurrent
                # session, including audit writes, websocket progress
                # streams, and unrelated users.  We wrap the exception
                # into a typed ComposerPluginCrashError that surfaces
                # to the operator as an HTTP 500 with
                # ``type(exc).__name__`` in the structured log, and
                # preserves the original on ``__cause__`` for the ASGI
                # error machinery.  The handler directly above
                # re-raises the narrow set of exception classes that
                # MUST NOT be laundered, so the concession below is
                # bounded.
                #
                # Wrap narrow-scope: only exceptions from the
                # execute_tool call are wrapped here. Bugs in
                # _call_llm_before_deadline / _build_messages surface
                # through their own exception classes
                # (ComposerServiceError, ComposerConvergenceError).
                # Record PLUGIN_CRASH BEFORE raising — done structurally
                # by ``dispatch_with_audit``. The ``capture()`` helper
                # takes the recorder buffer (including the helper's
                # final crash record) so the route handler's
                # ``_handle_plugin_crash`` gets the complete sequence.
                if failure_custody is not None and (failure_custody.errors or observation is None or not observation.execution_failed):
                    combined = failure_custody.failure(tool_exc)
                    if combined is tool_exc:
                        raise
                    raise combined from combined.__cause__
                _append_tool_outcome(
                    response=None,
                    error_class=type(tool_exc).__name__,
                    error_category=None,
                    error_message=type(tool_exc).__name__,
                    post_version=state.version,
                )
                plugin_crash = ComposerPluginCrashError.capture(
                    tool_exc,
                    state=state,
                    initial_version=initial_version,
                    tool_invocations=recorder.invocations,
                    llm_calls=recorder.llm_calls,
                )
                plugin_crash_cause = tool_exc
                if failure_custody is not None:
                    failure_custody.promote_completed_failure(kind="plugin_crash", original=tool_exc, wrapper=plugin_crash)
                break

            # SUCCESS path — the helper already recorded
            # ComposerToolStatus.SUCCESS via finish_success (with the
            # sentinel-canonical fallback for non-finite floats /
            # non-serializable result types). Update loop-local state
            # from outcome.result and continue with the LLM-message
            # append.
            version_before_tool = state.version
            admitted_result = outcome.result if isinstance(outcome.result, AdmittedDiscoveryResult) else None
            result = admitted_result.result if admitted_result is not None else outcome.result
            if (
                prevalidated_unapplied_result is None
                and is_mutation_tool(tool_name)
                and result.success
                and result.updated_state.version > state.version
                and result.validation.is_valid
            ):
                # The mutation has succeeded and its applied version is already
                # audited. A separate preflight failure cannot relabel that tool
                # as a crash or erase the version the route must persist.
                try:
                    findings = await _pending_review_runtime_findings(result.updated_state, ctx)
                except ComposerRuntimePreflightError as preflight_exc:
                    raise ComposerRuntimePreflightError(
                        original_exc=preflight_exc.original_exc,
                        partial_state=preflight_exc.partial_state,
                        tool_invocations=recorder.invocations,
                        llm_calls=recorder.llm_calls,
                        failed_turn=preflight_exc.failed_turn or ctx.failed_turn,
                    ) from preflight_exc.original_exc
                if findings is not None:
                    result = replace(result, runtime_preflight=findings)
            if prevalidated_unapplied_result is None:
                state = result.updated_state
                last_validation = result.validation
                last_runtime_preflight = result.runtime_preflight or last_runtime_preflight
            # Mark the (plugin_type, plugin_name) pair as
            # schema-loaded for this session when a get_plugin_schema
            # call returned a discovery success. The per-turn system
            # context renders this set as
            # ``composer_progress.schemas_loaded_this_session`` so
            # the model can compute its own schemas_gap without
            # re-introspecting plugins it has already seen. See
            # ``SchemaDisclosureTracker.mark_plugin_schema_loaded`` and
            # ``prompts.build_context_string``.
            #
            # Lifted from the prior inline compose-loop body into the
            # extracted ``_dispatch_tool_batch`` helper as part of the
            # parallel decomposition (commit f918f4269). The RC5.2
            # commit that introduced this tracker explicitly anticipated
            # the lift in its message: "the single line that will need
            # lift-and-shift into ``dispatch_tools`` when the parallel
            # ``_compose_loop`` decomposition lands".
            if tool_name == "get_plugin_schema" and result.success:
                ctx.schema_disclosure.mark_plugin_schema_loaded(
                    session_id,
                    str(arguments["plugin_type"]),
                    str(arguments["name"]),
                )
            # §7.7 anchor tracking. ``finish_success`` records the audit
            # invocation regardless of ``result.success`` — the dispatch
            # itself ran without raising. But for anchor purposes we look
            # at ToolResult.success: a set_pipeline that returned a
            # validation-rejected state is the dominant anchor pattern
            # observed in the Tier 1 RED, and indistinguishable from a
            # ToolArgumentError as far as the LLM's retry loop is concerned.
            #
            # Successes only break the anchor when they are MUTATION
            # successes. Discovery successes (get_plugin_schema, list_*,
            # get_pipeline_state) are observations — empirically the model
            # interleaves them between failed mutation retries (see the
            # smoke session 55895523-... where 4 set_pipeline failures
            # were broken up by 1 get_pipeline_state and 1 get_plugin_schema,
            # both successful, both irrelevant to whether the model has
            # progressed on the anchored mutation).
            #
            # Mutation means a CompositionState version advance here.
            # Blob-store side effects such as create_blob/update_blob are
            # useful work, but they do not make an empty pipeline exist.
            if prevalidated_unapplied_result is not None:
                anti_anchor.record_failure(tool_name, audit.arguments_hash)
            elif result.success:
                if _tool_result_mutated_composition_state(version_before=version_before_tool, result=result):
                    mutation_success_observed = True
                    anti_anchor.record_success()
            else:
                anti_anchor.record_failure(tool_name, audit.arguments_hash)
            result_json = (
                serialize_admitted_discovery_result(admitted_result) if admitted_result is not None else _serialize_tool_result(result)
            )
            if (
                tool_name in {"get_blob_content", "read_attachment", "inspect_source"}
                and admitted_result is not None
                and ctx.turn_evidence is not None
            ):
                ctx.turn_evidence.register_blob_page(
                    tool_call_id=tool_call.id,
                    admitted=admitted_result,
                    selected_content=result_json,
                    producer_call_id=ctx.turn_evidence.current_call_id("primary"),
                )
            await emit_progress(progress, tool_completed_progress_event(tool_name, result.success))
            _append_tool_outcome(
                response=admitted_result.to_tool_result() if admitted_result is not None else result,
                error_class=None,
                error_category=None,
                error_message=None,
                post_version=state.version,
            )

            # Cached payloads omit rejection entries; only successful discovery is reusable.
            if is_cacheable_discovery_tool(tool_name) and result.success:
                cache_key = _make_cache_key(tool_name, arguments)
                discovery_cache[cache_key] = _cached_discovery_payload(admitted_result if admitted_result is not None else result)

            result_message = tool_result_message(
                call_id=tool_call.id,
                result=admitted_result if admitted_result is not None else result,
                serialized=result_json,
            )
            if ctx.message_producers is not None and tool_name == "get_plugin_schema" and result.success:
                ctx.message_producers.require_context(session_id=session_id, context=ctx.session_operation_context, actor_user_id=user_id)
                schema_admitted = admitted_result if admitted_result is not None else admit_discovery_result(tool_name, result)
                ctx.message_producers.register_schema_message(call=tool_call, admitted=schema_admitted, message=result_message)
            if retained_attachment is not None:
                if ctx.message_producers is None or admitted_result is None:
                    raise AuditIntegrityError("retained attachment result lost its actual current message producer")
                from elspeth.web.composer.continuation_codec import encode_continuation_response

                # Actual original completion/audit; the caller binds this exact
                # response to the retained selected ToolBatchFrame before entry.
                response_source = encode_continuation_response(call_model.completion, audit_call=call_model.audit_call)
                ctx.message_producers.register_attachment_message(
                    response=response_source, call=tool_call, result=admitted_result.to_tool_result(), message=result_message
                )
            elif ctx.message_producers is not None and not (tool_name == "get_plugin_schema" and result.success):
                ctx.message_producers.require_context(session_id=session_id, context=ctx.session_operation_context, actor_user_id=user_id)
                ctx.message_producers.register_tool_result_message(
                    completion=call_model.completion,
                    tool_index=call.index,
                    call=tool_call,
                    result=admitted_result if admitted_result is not None else result,
                    serialized=result_json,
                    message=result_message,
                    catalog=ctx.policy_catalog,
                )
            llm_messages.append(result_message)

            if not is_discovery_tool(tool_name):
                turn_has_mutation = True
            else:
                turn_has_discovery = True

            if acc.authority_revocation is not None:
                break

            # The completed preview has its ordinary audit, message, outcome and
            # classification. An unavailable saved claim closes this issued batch;
            # P4 will persist only this executed prefix before the loop terminates.
            if admitted_result is not None and preview_source_proof_stop(admitted_result, state) is not None:
                break

        else:
            ran_to_end = True
    except BaseException as error:
        if failure_custody is not None:
            if failure_custody.original is None:
                failure_custody.observe_primary(error)
            combined = failure_custody.failure(error)
            if combined is error:
                raise
            raise combined from combined.__cause__
        raise
    finally:
        batch.state = state
        batch.last_validation = last_validation
        batch.last_runtime_preflight = last_runtime_preflight
        batch.advisor_calls_used = advisor_calls_used
        batch.turn_has_mutation = turn_has_mutation
        batch.turn_has_discovery = turn_has_discovery
        batch.all_cache_hits = all_cache_hits
        batch.tool_outcomes = tool_outcomes
        if failure_custody is None:
            batch.plugin_crash = plugin_crash
            batch.plugin_crash_cause = plugin_crash_cause
            batch.advisor_failure = advisor_failure
        batch.service_failure = service_failure
        batch.advisor_compose_timeout = advisor_compose_timeout
        batch.pre_state_id = pre_state_id
        batch.decoded_args_by_call_id = decoded_args_by_call_id
        batch.private_authorings_by_call = private_authorings_by_call
        batch.proposals_this_turn = proposals_this_turn
        batch.mutation_success_observed = mutation_success_observed
        batch.refused_resources = refused_resources
        batch.promises_by_call = promises_by_call
        call.audit = audit
        call.arguments = arguments
        call.audit_arguments = audit_arguments
    if len(tool_outcomes) != before + 1:
        raise AuditIntegrityError("completed tool call has no exact single actual outcome")
    call.phase = "applied"
    batch.stop_issued_batch = batch.stop_issued_batch or not ran_to_end
    return "applied"''',
    "batch._finish_continuation_batch_live": r'''def _finish_continuation_batch_live(
    *,
    call_model: _CallModelOutcome,
    ctx: ToolBatchContext,
    acc: BatchAccumulator,
    batch: ContinuationBatchState,
    failure_custody: ContinuationToolFailureCustody | None = None,
) -> tuple[_DispatchOutcome, int]:
    """Project the actual complete issued batch and executed prefix."""
    state = batch.state
    last_validation = batch.last_validation
    last_runtime_preflight = batch.last_runtime_preflight
    tool_outcomes = batch.tool_outcomes
    decoded_args_by_call_id = batch.decoded_args_by_call_id
    private_authorings_by_call = batch.private_authorings_by_call
    turn_has_mutation = batch.turn_has_mutation
    turn_has_discovery = batch.turn_has_discovery
    all_cache_hits = batch.all_cache_hits
    plugin_crash = batch.plugin_crash
    plugin_crash_cause = batch.plugin_crash_cause
    advisor_failure = batch.advisor_failure
    service_failure = batch.service_failure
    if failure_custody is not None and failure_custody.completed:
        if failure_custody.batch is not batch:
            raise AuditIntegrityError("batch finish changed its original native failure custody")
        plugin_crash = failure_custody.wrapper
        plugin_crash_cause = failure_custody.original if plugin_crash is not None else None
        advisor_origin = failure_custody.original
        if batch.service_failure is not None:
            if advisor_origin is not batch.service_failure:
                raise AuditIntegrityError("service finish replaced its actual native original")
            service_failure = batch.service_failure
        if batch.retained_failure is not None and batch.retained_failure.kind == "advisor_failure":
            if not isinstance(advisor_origin, Exception):
                raise AuditIntegrityError("Advisor finish lost its actual handled exception")
            advisor_failure = advisor_origin
    advisor_compose_timeout = batch.advisor_compose_timeout
    mutation_success_observed = batch.mutation_success_observed
    pre_state_id = batch.pre_state_id
    advisor_calls_used = batch.advisor_calls_used
    recorder = ctx.recorder
    completion = call_model.completion
    assistant_message = completion.message
    raw_assistant_content = assistant_message.content
    assistant_tool_calls = completion.tool_batch.calls
    outcome_ids = {outcome.call.id for outcome in tool_outcomes}
    dispatch = _DispatchOutcome(
        state=state,
        last_validation=last_validation,
        last_runtime_preflight=last_runtime_preflight,
        tool_outcomes=tuple(tool_outcomes),
        decoded_args_by_call_id=decoded_args_by_call_id,
        private_authorings=tuple(private_authorings_by_call[outcome.call.id] for outcome in tool_outcomes),
        private_invocations=tuple(invocation for invocation in recorder.invocations if invocation.tool_call_id in outcome_ids),
        turn_has_mutation=turn_has_mutation,
        turn_has_discovery=turn_has_discovery,
        all_cache_hits=all_cache_hits,
        plugin_crash=plugin_crash,
        plugin_crash_cause=plugin_crash_cause,
        advisor_failure=advisor_failure,
        service_failure=service_failure,
        advisor_compose_timeout=advisor_compose_timeout,
        assistant_message=assistant_message,
        raw_assistant_content=raw_assistant_content,
        assistant_tool_calls=assistant_tool_calls,
        mutation_success_observed=mutation_success_observed,
        pre_state_id=pre_state_id,
        authority_revocation=acc.authority_revocation,
    )
    return dispatch, advisor_calls_used''',
    "batch.ContinuationBatchState": r"""@dataclass(slots=True)
class ContinuationBatchState:
    state: CompositionState
    last_validation: ValidationSummary | None
    last_runtime_preflight: ValidationResult | None
    advisor_calls_used: int
    entry_state: CompositionState
    early_reviewed: bool = False
    phase: Literal["uninitialized", "call_ready", "call_active", "dispatch_ready", "p4_pending", "p4_result_retained", "complete"] = (
        "uninitialized"
    )
    next_tool_index: int = 0
    assistant_message_index: int | None = None
    mixed_abandonment_batch: bool = False
    provider_model_version: str = ""
    turn_has_mutation: bool = False
    turn_has_discovery: bool = False
    all_cache_hits: bool = True
    tool_outcomes: list[_ToolOutcome] = field(default_factory=list)
    plugin_crash: ComposerPluginCrashError | None = None
    plugin_crash_cause: BaseException | None = None
    advisor_failure: Exception | None = None
    service_failure: _ServiceFailure | None = None
    advisor_compose_timeout: Literal["pre_call", "in_flight"] | None = None
    pre_state_id: str | None = None
    decoded_args_by_call_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    private_authorings_by_call: dict[str, PrivateToolAuthoring] = field(default_factory=dict)
    proposals_this_turn: int = 0
    mutation_success_observed: bool = False
    refused_resources: set[Resource] = field(default_factory=set)
    promises_by_call: dict[str, frozenset[Resource]] = field(default_factory=dict)
    current_call: ContinuationCallState | None = None
    stop_issued_batch: bool = False
    p4: _PersistOutcome | None = None
    retained_failure: RetainedBatchFailure | None = None
    p4_receipt_json: str | None = None
    p4_input_json: str | None = None
    p4_verification_failed: bool = False
    # Live-only reservation owner. The continuation codec retains the spent
    # integer above; a checkpoint is taken only after the active stage closes.
    manual_advisor_allowance: ManualAdvisorAllowance | None = field(default=None, repr=False)""",
    "batch.ContinuationCallState": r"""@dataclass(slots=True)
class ContinuationCallState:
    index: int
    tool_call: _AdmittedToolCall
    pre_state: CompositionState
    pre_version: int
    phase: Literal["unprepared", "prepared", "owner_result_retained", "application_pending", "applied"] = "unprepared"
    tool_name: str = ""
    private_raw_arguments: str = ""
    private_arguments_canonical: str | None = None
    private_authority_canonical: str | None = None
    unknown_audit_arguments: dict[str, Any] | None = None
    wire: _CallWireFacts = field(default_factory=_CallWireFacts)
    arguments: dict[str, Any] | None = None
    audit_arguments: Mapping[str, Any] | str | None = None
    audit: DispatchAudit | None = None
    effect_key: str | None = None
    owner_result_kind: Literal["proposal", "persisted_turn", "read", "interpretation"] | None = None
    owner_result_json: str | None = None
    memory_result: ToolResult | AdmittedDiscoveryResult | None = None
    original_creator_id: str | None = None
    original_creator_epoch: int | None = None
    completion_json: str | None = None
    owner_tool_receipt_json: str | None = None
    advisor_invocation_json: str | None = None
""",
    "batch._CallWireFacts": r'''@dataclass(slots=True)
class _CallWireFacts:
    """One tool call's wire facts, filled in as ``run_tool_batch`` learns them.

    Deliberately mutable: it is created at the top of each call's iteration
    and captured by that iteration's ``_append_tool_outcome`` as a default
    argument, and decode runs after the closure is defined, so a holder
    avoids late binding through a loop variable. ``strict_sent`` follows
    D16 (the ``strict`` key sent for the tool, ``None`` when none was sent)
    and ``wire_conformant`` is ``None`` exactly where no arguments were
    decoded.
    """

    strict_sent: bool | None = None
    wire_conformant: bool | None = None
''',
    "batch.ToolBatchContext": r'''@dataclass(frozen=True, slots=True)
class ToolBatchContext:
    """Loop-invariant inputs to the dispatch loop, built once per batch.

    ``frozen=True`` prevents rebinding the field *references* only. The
    ``discovery_cache`` and ``runtime_preflight_cache`` fields are
    intentionally-mutable shared caches the dispatch loop writes into; they
    are deliberately exempt from the project's ``deep_freeze`` contract
    because deep-freezing them would break the loop's cache-write behaviour.
    No ``__post_init__`` freeze guard is added for that reason.
    """

    session_tools: SessionToolOwner
    provenance: ToolBatchProvenance
    custody: ToolBatchCustody
    redaction_telemetry: RedactionTelemetry
    preflight: ComposerPreflight
    schema_disclosure: SchemaDisclosureTracker
    advisor_checkpoint: AdvisorCheckpointOwner
    advisor_max_calls_per_compose: int
    advisor_timeout_seconds: float
    recorder: BufferingRecorder
    anti_anchor: AntiAnchorTracker
    discovery_cache: dict[str, _CachedDiscoveryPayload]
    runtime_preflight_cache: _RuntimePreflightCache
    session_id: str | None
    session_operation_context: SessionOperationContext | None
    user_id: str | None
    user_message_id: str | None
    user_message_content: str | None
    current_state_id: str | None
    actor: str
    initial_version: int
    deadline: float
    progress: ComposerProgressSink | None
    session_scope: str
    turn_sessions_service: SessionServiceProtocol | None
    turn_session_uuid: UUID | None
    turn_preferences: ComposerSessionPreferencesRecord | None
    mutation_ceiling: ComposerMutationCeiling | None
    review_required: bool
    cancellation_requested: asyncio.Event
    plugin_snapshot: PluginAvailabilitySnapshot
    policy_catalog: PolicyCatalogView
    # The dialect the loop's tool list was sent under on this call. The
    # service resolves it once and builds both the sent list and this context
    # from the same value, so decode always reads the W that was sent.
    tool_contract_dialect: ToolContractDialect
    turn_evidence: TurnEvidence | None = None
    session_operation_authority: SessionOperationAuthority | None = None
    provider_owner: ProviderInvocationOwner | None = None
    # Driver turn counters and last persisted tool-call turn, read only when
    # the batch raises a ComposerConvergenceError (``turns_used`` and
    # ``failed_turn`` on the 422 body), exactly as ``_enforce_tool_call_cap``
    # reports them.
    composition_turns_used: int = 0
    discovery_turns_used: int = 0
    failed_turn: FailedTurnMetadata | None = None
    message_producers: CurrentContinuationMessageProducers | None = None
''',
    "batch.BatchAccumulator": r'''@dataclass(slots=True)
class BatchAccumulator:
    """Driver-owned state threaded across compose-loop turns.

    The four value inputs seed the live ContinuationBatchState once; the
    driver receives their updates through _DispatchOutcome. The original
    accumulator itself remains the mutation-revocation owner during execution
    and final projection. Per-batch values live on ContinuationBatchState,
    with explicit local aliases and finally copyback around each call stage.
    """

    state: CompositionState
    last_validation: ValidationSummary | None
    last_runtime_preflight: ValidationResult | None
    advisor_calls_used: int
    authority_revocation: ComposerMutationRevocation | None = None
''',
    "batch._retain_llm_tool_argument_parts": r'''def _retain_llm_tool_argument_parts(
    llm_messages: Sequence[dict[str, Any]],
    *,
    tool_call_id: str,
    ctx: ToolBatchContext | None = None,
    call_model: _CallModelOutcome | None = None,
) -> None:
    """Attach all-untrusted semantic units only after argument admission."""
    for message in reversed(llm_messages):
        if message["role"] != "assistant" or "tool_calls" not in message:
            continue
        for index, call in enumerate(message["tool_calls"]):
            if call["id"] != tool_call_id:
                continue
            entry = PromptGuardToolArgumentParts(index, json_argument_content_parts(call["function"]["arguments"]))
            existing = (
                cast(tuple[PromptGuardToolArgumentParts, ...], message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY])
                if PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY in message
                else ()
            )
            message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] = tuple(
                sorted((*[part for part in existing if part.tool_call_index != index], entry), key=lambda part: part.tool_call_index)
            )
            if ctx is not None:
                if call_model is None:
                    raise AuditIntegrityError("argument registration lost its actual provider outcome")
                _register_continuation_assistant_message(ctx, call_model, message, tool_call_id=tool_call_id)
            return
    raise AuditIntegrityError("Admitted tool argument was not present in the active LLM transcript")
''',
    "advisor._validate_advisor_arguments": r'''def _validate_advisor_arguments(self, arguments: dict[str, Any]) -> RequestAdvisorHintArgumentsModel | AdvisorArgumentRejection:
    """Admit complete public input before advisor budget or provider effects.

    The arguments are held to the tool's closed-root flat schema S first,
    exactly as every ``execute_tool`` dispatch is, then to the pydantic
    model, then to the prompt-size cap. Each rejection records the class
    actually raised (or constructed) and its closed category.
    """
    schema_error_text = "request_advisor_hint arguments must conform to the published schema; check field types, limits, and extra keys"
    try:
        require_schema_valid_arguments("request_advisor_hint", arguments)
    except ToolArgumentError as exc:
        return AdvisorArgumentRejection(error=schema_error_text, error_class=type(exc).__name__, category=exc.category)
    try:
        validated = RequestAdvisorHintArgumentsModel.model_validate(arguments)
    except PydanticValidationError as exc:
        return AdvisorArgumentRejection(
            error=schema_error_text,
            error_class=type(exc).__name__,
            category=ToolArgumentErrorCategory.MODEL_VALIDATION,
        )

    # Approximate provider cost cap: rough 4 chars / token. Compute the
    # exact formatted user-message char count we would emit if the call
    # proceeded, including section labels, bullets, and newlines. The
    # fixed system side is bounded separately by the packaged skill plus
    # load_deployment_skill's byte cap; this setting bounds the
    # LLM-controlled variable part.
    total_chars = len(_advisor_context.build_advisor_user_message(validated.to_internal_request()))
    char_cap = self._settings.composer_advisor_max_prompt_tokens * _advisor_context.ADVISOR_CHARS_PER_TOKEN
    if total_chars > char_cap:
        # Nothing is raised by the cap check; the rejection it records is
        # the owned ToolArgumentError built here, so its class is honest.
        budget_rejection = ToolArgumentError(
            argument="request_advisor_hint arguments",
            expected="a prompt within composer_advisor_max_prompt_tokens",
            actual_type="prompt over budget",
            category=ToolArgumentErrorCategory.PROMPT_BUDGET,
        )
        return AdvisorArgumentRejection(
            error=(
                f"prompt size {total_chars} chars exceeds cap {char_cap} chars "
                f"(composer_advisor_max_prompt_tokens={self._settings.composer_advisor_max_prompt_tokens}). "
                "Truncate your error/action lists or schema excerpt and retry."
            ),
            error_class=type(budget_rejection).__name__,
            category=budget_rejection.category,
        )

    return validated
''',
    "canonical._normalize_value": r'''def _normalize_value(obj: Any) -> Any:
    """Convert a single value to JSON-safe primitive.

    Handles pandas and numpy types that appear in real pipeline data.

    NaN Policy: STRICT REJECTION
    - NaN and Infinity are invalid input states for float AND Decimal
    - Use None/pd.NA/NaT for intentional missing values
    - This prevents silent data corruption in audit records

    Args:
        obj: Any Python value

    Returns:
        JSON-serializable primitive

    Raises:
        ValueError: If value contains NaN or Infinity
    """
    # Check for NaN/Infinity FIRST (before type coercion)
    if isinstance(obj, np.floating):
        # Use NumPy-native finiteness check — math.isnan/isinf implicitly
        # downcast through float(), which overflows for np.longdouble values
        # outside IEEE 754 double range (falsely treating finite values as inf).
        if not np.isfinite(obj):
            raise ValueError(f"Cannot canonicalize non-finite float: {obj}. Use None for missing values, not NaN.")
        converted = float(obj)
        if not math.isfinite(converted):
            raise ValueError(
                f"Cannot canonicalize {type(obj).__name__} value: exceeds IEEE 754 double range. "
                f"Value is finite in native representation but overflows JSON number format."
            )
        return converted
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise ValueError(f"Cannot canonicalize non-finite float: {obj}. Use None for missing values, not NaN.")
        return obj

    # Primitives pass through unchanged
    if obj is None or isinstance(obj, str | int | bool):
        return obj

    # NumPy scalar types
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        # Numeric arrays can be checked vectorially; object/string arrays are
        # normalized element-by-element below so embedded Python floats still
        # use the scalar non-finite checks.
        if obj.size > 0 and np.issubdtype(obj.dtype, np.number) and not np.all(np.isfinite(obj)):
            raise ValueError(
                "NaN/Infinity found in NumPy array. Audit trail requires finite values only. Use None for missing values, not NaN."
            )
        # 0-D arrays convert to scalars via tolist()/item(); normalize that scalar directly.
        if obj.ndim == 0:
            return _normalize_value(obj.item())
        return [_normalize_value(x) for x in obj.tolist()]

    # NumPy datetime64
    if isinstance(obj, np.datetime64):
        if np.isnat(obj):
            return None
        # Convert to pd.Timestamp for consistent UTC ISO 8601 output
        ts = pd.Timestamp(obj)
        if ts.tz is None:
            return ts.tz_localize("UTC").isoformat()
        return ts.tz_convert("UTC").isoformat()

    # Pandas types
    if isinstance(obj, pd.Timestamp):
        # Naive timestamps assumed UTC (explicit policy)
        if obj.tz is None:
            return obj.tz_localize("UTC").isoformat()
        return obj.tz_convert("UTC").isoformat()

    # Intentional missing values (NOT NaN - that's rejected above).
    # Both are singletons, so identity IS the type check: ``obj is pd.NaT``
    # already implies ``isinstance(obj, type(pd.NaT))``.
    if obj is pd.NA or obj is pd.NaT:
        return None

    # Standard library types
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=UTC)
        return obj.astimezone(UTC).isoformat()

    if isinstance(obj, date):
        return obj.isoformat()

    if isinstance(obj, time):
        return obj.isoformat()

    if isinstance(obj, UUID):
        return str(obj)

    if isinstance(obj, bytes):
        return {
            _CANONICAL_TYPE_KEY: _CANONICAL_BYTES_TAG,
            "base64": base64.b64encode(obj).decode("ascii"),
        }

    if isinstance(obj, Decimal):
        if not obj.is_finite():  # Rejects NaN, sNaN, Infinity, -Infinity
            raise ValueError(f"Cannot canonicalize non-finite Decimal: {obj}. Use None for missing values, not NaN/Infinity.")
        return str(obj)

    return obj
''',
    "canonical._normalize_for_canonical": r'''def _normalize_for_canonical(data: Any) -> Any:
    """Recursively normalize a data structure for canonical JSON.

    Converts pandas/numpy types to JSON-safe primitives.
    Handles PipelineRow by converting to dict.

    Args:
        data: Any data structure (dict, list, primitive, PipelineRow)

    Returns:
        Normalized data structure with only JSON-safe types

    Raises:
        ValueError: If data contains NaN, Infinity, or other non-serializable values
    """
    # Handle PipelineRow - convert to dict before processing
    if isinstance(data, PipelineRow):
        data = data.to_dict()

    if isinstance(data, Mapping):
        if _CANONICAL_TYPE_KEY in data:
            entries = [
                {
                    "key": _normalize_for_canonical(key),
                    "value": _normalize_for_canonical(value),
                }
                for key, value in data.items()
            ]
            return {
                _CANONICAL_TYPE_KEY: _CANONICAL_MAPPING_TAG,
                "entries": sorted(entries, key=lambda entry: _primitive_canonical_json(entry["key"])),
            }
        return {k: _normalize_for_canonical(v) for k, v in data.items()}
    if isinstance(data, list | tuple):
        return [_normalize_for_canonical(v) for v in data]
    return _normalize_value(data)
''',
    "canonical.canonical_json": r'''def canonical_json(obj: Any) -> str:
    """Produce canonical JSON for hashing.

    Two-phase approach:
    1. Normalize pandas/numpy types to JSON-safe primitives (our code)
    2. Serialize via the shared contracts.hashing primitive policy

    Args:
        obj: Data structure to serialize

    Returns:
        Canonical JSON string (no whitespace, sorted keys)

    Raises:
        ValueError: If data contains NaN, Infinity, or other non-finite values
        TypeError: If data contains types that cannot be serialized
    """
    normalized = _normalize_for_canonical(obj)
    return _primitive_canonical_json(normalized)
''',
    "canonical.stable_hash": r'''def stable_hash(obj: Any) -> str:
    """Compute stable hash of object.

    Args:
        obj: Data structure to hash

    Returns:
        SHA-256 hex digest of canonical JSON
    """
    normalized = _normalize_for_canonical(obj)
    return _primitive_stable_hash(normalized)
''',
    "required._optional_ancestor_present": r'''@trust_boundary(
    tier=3,
    source="LLM-emitted tool-call arguments (untrusted model output JSON)",
    source_param="value",
    suppresses=("R5",),
    invariant=(
        "returns False when any ancestor segment is absent or the cursor is not a mapping; never raises on "
        "malformed value input (the NotImplementedError guard is conditioned on the first-party compiled ancestor, not on value)"
    ),
    non_raising=True,
)
def _optional_ancestor_present(value: object, ancestor: RequiredPath) -> bool:
    """Walk down ``value`` along ``ancestor``; return False as soon as a segment is absent.

    Empty ``ancestor`` is the always-required case — treated as present.

    Today ``_collect_required_paths`` only sets ``optional_ancestor`` to a new
    path when descending into an OBJECT property that's not in ``required``;
    descending into array items propagates the existing ancestor unchanged.
    So array segments never appear in ``optional_ancestor`` under the current
    schema set. A future schema with an optional sub-object inside array items
    (e.g., ``tags: array<{ details?: { name: required } }>``) WOULD produce
    such an ancestor, and the all-or-nothing semantics here can't express
    "present in some items, absent in others." Per the engine-patterns-reference
    skill §Offensive Programming Examples: crash loudly with a diagnostic that
    points the maintainer at the extension site, rather than silently producing
    wrong validation.
    """
    if not ancestor:
        return True
    cursor: object = value
    for segment in ancestor:
        if segment == _ARRAY_ITEM_SEGMENT:
            raise NotImplementedError(
                "Array-segment in optional_ancestor is not yet supported by this walker. "
                f"Saw ancestor={ancestor!r}. To handle optional sub-objects inside array "
                "items, extend _find_missing_required_paths to evaluate ancestor presence "
                "per-array-item (rather than once globally) and update this walker to "
                "descend through array segments accordingly."
            )
        if not isinstance(cursor, Mapping) or segment not in cursor:
            return False
        cursor = cursor[segment]
    return True
''',
    "required._find_missing_path_instances": r'''def _find_missing_path_instances(
    value: object,
    required_path: RequiredPath,
    *,
    current_path: str = "",
) -> list[str]:
    """Return concrete missing-path instances for one required path."""
    if not required_path:
        return []

    head = required_path[0]
    tail = required_path[1:]

    if head == _ARRAY_ITEM_SEGMENT:
        match value:
            case list() as items:
                missing_paths: list[str] = []
                for index, item in enumerate(items):
                    item_path = f"{current_path}[{index}]" if current_path else f"[{index}]"
                    missing_paths.extend(_find_missing_path_instances(item, tail, current_path=item_path))
                return missing_paths
            case _:
                return []

    match value:
        case dict() as mapping:
            next_path = f"{current_path}.{head}" if current_path else head
            if head not in mapping:
                return [next_path]
            return _find_missing_path_instances(mapping[head], tail, current_path=next_path)
        case _:
            return []
''',
    "required._find_missing_required_paths": r'''def _find_missing_required_paths(
    value: object,
    required_paths: tuple[_CompiledRequiredPath, ...],
) -> list[str]:
    """Return dotted/indexed paths for missing schema-required fields.

    Skips any compiled path whose ``optional_ancestor`` is absent in the
    value: that mirrors JSON-Schema semantics where nested ``required``
    only applies when the containing optional object is itself present.
    """
    missing_paths: list[str] = []
    for compiled in required_paths:
        if not _optional_ancestor_present(value, compiled.optional_ancestor):
            continue
        missing_paths.extend(_find_missing_path_instances(value, compiled.path))
    return missing_paths


# Computed once at import time. Imports of this module trigger
# ``_build_tool_required_paths_index()`` which iterates the tool registry —
# a malformed tool definition will crash at import (intentional).
''',
    "resource.promised_resources": r'''@trust_boundary(
    tier=3,
    source="bounded provider JSON tool arguments, before tool-specific schema admission",
    source_param="arguments",
    suppresses=("R1",),
    invariant="extracts only explicitly supplied string resources from recognized structural fields; ignores malformed field shapes",
    non_raising=True,
)
def promised_resources(tool: str, arguments: Mapping[str, JsonValue]) -> frozenset[Resource]:
    """Named resources explicitly offered by a call, whether admitted or not."""
    resources: set[Resource] = set()
    if tool == "upsert_node":
        resources.update(_node_produces(arguments))
    elif tool in ("set_source", "set_source_from_blob", "set_source_from_blobs"):
        _add(resources, "component", arguments.get("source_name", "source"))
        _add(resources, "source", arguments.get("source_name", "source"))
        _add(resources, "connection", arguments.get("on_success"))
    elif tool == "set_output":
        _add(resources, "component", arguments.get("sink_name"))
        _add(resources, "output", arguments.get("sink_name"))
    elif tool == "upsert_edge":
        _add(resources, "edge", arguments.get("id"))
    elif tool == "splice_transform":
        resources.update(_node_produces(_object(arguments.get("node"))))
    elif tool == "set_pipeline":
        source = _object(arguments.get("source"))
        if source:
            resources.update(promised_resources("set_source", source))
        for name, source_value in _object(arguments.get("sources")).items():
            resources.update(promised_resources("set_source", {**_object(source_value), "source_name": name}))
        for key, child_tool in (("nodes", "upsert_node"), ("edges", "upsert_edge"), ("outputs", "set_output")):
            children = arguments.get(key)
            if type(children) is list:
                for child in children:
                    resources.update(promised_resources(child_tool, _object(child)))
    return frozenset(resources)
''',
    "registry.resolve_tool_effects": r'''def resolve_tool_effects(tool_name: str, arguments: Mapping[str, Any]) -> ToolEffects:
    """Resolve a known tool's prospective effects without invoking its handler."""
    if tool_name in ASYNC_TOOL_EFFECTS:
        return ASYNC_TOOL_EFFECTS[tool_name]
    if tool_name not in _DECLARATIONS_BY_NAME:
        raise AssertionError("Unknown composer tool for effect resolution.")
    return _DECLARATIONS_BY_NAME[tool_name].resolve_effects(arguments)


# ---------------------------------------------------------------------------
# Derived tool-definitions map (consumed by ``_dispatch.get_tool_definitions``)
# ---------------------------------------------------------------------------
''',
    "freeze.deep_thaw": r'''def deep_thaw(value: Any) -> Any:
    """Recursively convert frozen containers to JSON-serializable mutable types.

    Converts ``MappingProxyType`` → ``dict`` and ``tuple`` → ``list``,
    recursing into values. Used by ``to_dict()`` methods to produce
    JSON-serializable output from deeply frozen fields.

    **Not a true inverse of ``deep_freeze``**: converts ALL tuples to
    lists, including tuples that were native (not converted from lists
    by ``deep_freeze``). This is intentional — JSON has no tuple type,
    so ``to_dict()`` callers need lists. For JSON-like input (only dicts
    and lists), ``deep_thaw(deep_freeze(x)) == x`` holds.

    Examples:
        >>> deep_thaw(MappingProxyType({"a": (1, MappingProxyType({"b": 2}))}))
        {'a': [1, {'b': 2}]}
    """
    if isinstance(value, MappingProxyType):
        return {k: deep_thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [deep_thaw(item) for item in value]
    if isinstance(value, frozenset):
        return [deep_thaw(item) for item in value]
    if isinstance(value, dict):
        return {k: deep_thaw(v) for k, v in value.items()}
    if isinstance(value, list):
        return [deep_thaw(item) for item in value]
    return value
''',
    "audit.build_canonicalization_sentinel": r'''def build_canonicalization_sentinel(
    exc: BaseException,
    payload: Any,
) -> dict[str, object]:
    """Build the sentinel that replaces a non-canonicalizable audit payload.

    The status-quo sentinel records only
    ``{"_canonicalization_error": "<exc class>"}`` which makes a
    recurrence opaque: an auditor reading
    ``result_canonical='{"_canonicalization_error":"CanonicalizationError"}'``
    cannot tell *what* failed without correlating with operational
    logs. This helper enriches the sentinel with bounded, leak-safe
    diagnostic metadata so the audit trail itself is sufficient
    forensic evidence.

    Sentinel shape:

    - ``_canonicalization_error`` — exception class name (status quo).
    - ``_canonicalization_detail`` — exception ``str(exc)`` only when
      ``exc`` is exactly the base :class:`rfc8785.CanonicalizationError`,
      whose three messages are type-name or JCS-rule strings
      (``"unsupported type: <class 'X'>"``, ``"object keys must be
      strings"``, ``"input contains non-UTF-8 codepoints"``). Its two
      subclasses echo the offending value, measured on rfc8785:
      ``IntegerDomainError`` is ``"<the integer> exceeds safe integer
      domain for JSON floats"`` and ``FloatDomainError`` is ``"<the
      float> is not representable in JCS"`` — so for them, as for every
      other ``ValueError`` / ``TypeError`` (``core/canonical.py``'s
      Decimal check interpolates the value too), the detail is omitted
      and the class name in ``_canonicalization_error`` says which rule
      failed. The payload is the planner's tool arguments or a tool
      result, which can carry values the planner copied from a user's
      data, and the sentinel is persisted in the Tier-1 audit row.
    - ``_payload_keys`` — sorted top-level keys of the failed payload
      when it is a Mapping. Keys are schema metadata (names like
      "data", "validation", "version"); values are NOT captured. This
      lets an auditor identify the failure shape (discovery-tool
      result vs compose-state mutation vs unparseable arguments)
      without leaking content.

    All captured metadata is bounded: no payload values, no exception
    messages from non-allowlisted classes, no recursion into nested
    structures.
    """
    sentinel: dict[str, object] = {"_canonicalization_error": type(exc).__name__}
    if type(exc) is rfc8785.CanonicalizationError:
        # Exactly the base class: its messages are type/rule strings. The
        # domain subclasses carry the value (see above). The 512-char cap
        # is belt-and-braces against a future rfc8785 inlining a longer
        # schema fragment.
        sentinel["_canonicalization_detail"] = str(exc)[:512]
    if isinstance(payload, Mapping):
        sentinel["_payload_keys"] = sorted(str(k) for k in payload)
    return sentinel
''',
    "audit.begin_dispatch": r'''def begin_dispatch(
    tool_call_id: str,
    tool_name: str,
    arguments: Mapping[str, Any] | str,
    *,
    version_before: int,
    actor: str,
    strict_sent: bool | None = None,
    wire_conformant: bool | None = None,
) -> DispatchAudit:
    """Open a per-call audit envelope.

    ``arguments`` is either a parsed dict (typical path) or a raw string
    (JSON-decode failure path before parse succeeded). For the raw-string
    case the canonical form wraps the (truncated) string in a sentinel
    object so the audit trail still records what the LLM tried even
    when it wasn't valid JSON. Truncation guards against unbounded
    audit-row sizes for pathological LLM output.

    ``strict_sent`` / ``wire_conformant`` are the caller's wire facts for
    this call. Callers that sent no wire schema leave the ``None`` defaults;
    the compose loop passes them.
    """
    if isinstance(arguments, str):
        # 4 KiB is the same boundary as POSIX PIPE_BUF — a sane upper
        # bound that captures normal tool-call shapes while preventing
        # a malformed multi-megabyte LLM response from blowing up audit
        # rows. The full string is recoverable from upstream LiteLLM
        # tracing if needed; the audit row records intent, not bytes.
        truncated = arguments[:4096]
        canon = canonical_json({"_unparseable_arguments": truncated, "_truncated": len(arguments) > 4096})
        h = stable_hash({"_unparseable_arguments": truncated, "_truncated": len(arguments) > 4096})
        authority_canon = None
        authority_hash = None
    else:
        canon = canonical_json(arguments)
        h = stable_hash(arguments)
        authority_canon = composer_authority_canonical_json(arguments) if tool_name == "set_pipeline" else None
        authority_hash = composer_authority_hash(arguments) if tool_name == "set_pipeline" else None
    return DispatchAudit(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        arguments_canonical=canon,
        arguments_hash=h,
        version_before=version_before,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        actor=actor,
        authority_arguments_canonical=authority_canon,
        authority_arguments_hash=authority_hash,
        strict_sent=strict_sent,
        wire_conformant=wire_conformant,
    )
''',
    "audit.begin_dispatch_or_arg_error": r'''def begin_dispatch_or_arg_error(
    tool_call_id: str,
    tool_name: str,
    arguments: Mapping[str, Any] | str,
    *,
    version_before: int,
    actor: str,
    strict_sent: bool | None = None,
    wire_conformant: bool | None = None,
) -> tuple[DispatchAudit, BaseException | None]:
    """Open an audit envelope without letting malformed args bypass audit.

    ``json.loads`` accepts non-standard constants like ``NaN`` and
    ``Infinity``. The canonicalizer rejects those values, which is
    correct, but that rejection can happen before the compose loop has an
    audit row. This helper mirrors the standalone MCP path: store a
    canonical sentinel for the arguments, return the canonicalization
    exception to the caller, and let the caller record ARG_ERROR.
    """
    try:
        return (
            begin_dispatch(
                tool_call_id,
                tool_name,
                arguments,
                version_before=version_before,
                actor=actor,
                strict_sent=strict_sent,
                wire_conformant=wire_conformant,
            ),
            None,
        )
    except (ValueError, TypeError) as exc:
        # Pass ``arguments`` only when it is a Mapping — the raw-string
        # path goes through ``begin_dispatch`` directly and never lands
        # here, but typing-narrow the call so ``_payload_keys`` is only
        # emitted when meaningful (sentinel for ``arguments`` that's a
        # str would emit a noisy index-key list).
        sentinel = build_canonicalization_sentinel(
            exc,
            arguments if isinstance(arguments, Mapping) else None,
        )
        return (
            DispatchAudit(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments_canonical=canonical_json(sentinel),
                arguments_hash=stable_hash(sentinel),
                version_before=version_before,
                started_at=datetime.now(UTC),
                started_ns=time.monotonic_ns(),
                actor=actor,
                strict_sent=strict_sent,
                wire_conformant=wire_conformant,
            ),
            exc,
        )
''',
    "audit.DispatchAudit": r'''@dataclass(frozen=True, slots=True)
class DispatchAudit:
    """Per-call timing + canonical-arguments envelope for the audit trail.

    Captured at the start of every dispatch branch in
    :meth:`ComposerServiceImpl._compose_loop` so each branch's invocation
    record carries consistent ``started_at`` / ``arguments_canonical`` /
    ``version_before`` regardless of which path the call ultimately
    follows. The branch-specific finalizers (``finish_*``) read fields
    from here to construct the final :class:`ComposerToolInvocation`.

    ``strict_sent`` and ``wire_conformant`` are the wire facts the caller
    knew before dispatch (see :class:`ComposerToolInvocation`); every
    finalizer copies them onto the invocation. ``None`` means unknown or not
    applicable.
    """

    tool_call_id: str
    tool_name: str
    arguments_canonical: str
    arguments_hash: str
    version_before: int
    started_at: datetime
    started_ns: int
    actor: str
    authority_arguments_canonical: str | None = None
    authority_arguments_hash: str | None = None
    strict_sent: bool | None = None
    wire_conformant: bool | None = None

    @property
    def binding_arguments_hash(self) -> str:
        """Return the semantic binding when this dispatch defines one."""
        return self.authority_arguments_hash or self.arguments_hash
''',
    "bounded._preflight_raw_json": r"""def _preflight_raw_json(raw: str, *, label: str) -> None:
    try:
        raw_bytes = len(raw.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise JsonBoundaryError(f"{label} is not valid UTF-8 text") from exc
    if raw_bytes > JSON_MAX_TOTAL_UTF8_BYTES:
        raise JsonBoundaryError(f"{label} exceeds the {JSON_MAX_TOTAL_UTF8_BYTES}-byte JSON limit")

    # Reject excessive nesting before the recursive C decoder sees it. Braces
    # and brackets inside JSON strings are data, not containers.
    open_containers = 0
    in_string = False
    escaped = False
    for character in raw:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            if open_containers > JSON_MAX_DEPTH:
                raise JsonBoundaryError(f"{label} exceeds the {JSON_MAX_DEPTH}-level JSON depth limit")
            open_containers += 1
        elif character in "]}" and open_containers:
            open_containers -= 1
""",
    "bounded._validate_json_structure": r'''def _validate_json_structure(
    value: object,
    *,
    label: str,
    owned_string_type: type[str] | None = None,
) -> None:
    """Share fixed JSON bounds with one explicitly owned scalar boundary.

    The raw decoder uses the strict wrapper below. Internal callers may retain
    one exact nominal string type; its subclasses and nominal object keys are
    refused, and no value is converted or detached during validation.
    """
    budget = JsonTraversalBudget()
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        budget.check_depth(depth, label=label)
        if type(current) is dict:
            budget.consume_items(len(current), label=label)
            for key, child in current.items():
                if type(key) is not str:
                    raise JsonBoundaryError(f"{label} contains a non-string object key")
                budget.consume_text(key, label=label, enforce_string_limit=False)
                stack.append((child, depth + 1))
            continue
        if type(current) is list:
            budget.consume_items(len(current), label=label)
            stack.extend((child, depth + 1) for child in current)
            continue
        if type(current) is str or (owned_string_type is not None and type(current) is owned_string_type):
            budget.consume_text(current, label=label, enforce_string_limit=False)
            continue
        if current is None or type(current) in {bool, int}:
            continue
        if type(current) is float and math.isfinite(current):
            continue
        raise ValueError(f"{label} contains a value outside strict finite JSON")
''',
    "bounded._validate_decoded_json": r'''def _validate_decoded_json(value: Any, *, label: str) -> None:
    """Validate raw decoded values as exact finite JSON, without nominal types."""
    _validate_json_structure(value, label=label)
''',
    "bounded.bounded_json_loads": r'''def bounded_json_loads(
    raw: object,
    *,
    label: str,
    object_pairs_hook: Callable[[list[tuple[str, Any]]], Any] | None = None,
) -> Any:
    """Decode exact JSON text under fixed byte, depth, item, and text bounds.

    Syntax errors remain ``JSONDecodeError`` so existing caller taxonomies keep
    their precise malformed-JSON class. Resource violations use the closed
    ``JsonBoundaryError`` class. ``object_pairs_hook`` is forwarded to the
    decoder so callers that must reject duplicate object keys keep doing so
    inside the same bounded decode.
    """
    if type(raw) is not str:
        raise TypeError(f"{label} must be an exact JSON string")
    _preflight_raw_json(raw, label=label)

    def reject_constant(_value: str) -> Any:
        # Preserve the pre-existing ordinary-dispatch audit taxonomy:
        # canonical non-finite values are ValueError, while depth/item/byte
        # resource violations are JsonBoundaryError.
        raise ValueError(f"{label} contains a non-finite JSON number")

    try:
        value = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=object_pairs_hook)
    except RecursionError as exc:
        raise JsonBoundaryError(f"{label} exceeds the JSON decoder recursion limit") from exc
    _validate_decoded_json(value, label=label)
    return value
''',
    "bounded.JsonTraversalBudget": r'''@dataclass(slots=True)
class JsonTraversalBudget:
    """One aggregate budget shared by a complete JSON-like traversal."""

    items: int = 0
    text_chars: int = 0
    utf8_bytes: int = 0

    def check_depth(self, depth: int, *, label: str) -> None:
        if depth > JSON_MAX_DEPTH:
            raise JsonBoundaryError(f"{label} exceeds the {JSON_MAX_DEPTH}-level JSON depth limit")

    def consume_items(self, count: int, *, label: str) -> None:
        if count < 0 or count > JSON_MAX_ITEMS - self.items:
            raise JsonBoundaryError(f"{label} exceeds the {JSON_MAX_ITEMS}-item JSON limit")
        self.items += count

    def consume_text(self, value: str, *, label: str, enforce_string_limit: bool = True) -> None:
        if enforce_string_limit and len(value) > JSON_MAX_STRING_CHARS:
            raise JsonBoundaryError(f"{label} contains a JSON string over {JSON_MAX_STRING_CHARS} characters")
        self.text_chars += len(value)
        if self.text_chars > JSON_MAX_TOTAL_TEXT_CHARS:
            raise JsonBoundaryError(f"{label} exceeds the aggregate JSON text limit")
        try:
            encoded_length = len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise JsonBoundaryError(f"{label} contains text that is not valid UTF-8") from exc
        self.utf8_bytes += encoded_length
        if self.utf8_bytes > JSON_MAX_TOTAL_UTF8_BYTES:
            raise JsonBoundaryError(f"{label} exceeds the aggregate JSON UTF-8 byte limit")
''',
    "wire.decode_wire_arguments": r'''@trust_boundary(
    tier=3,
    source="the JSON-decoded tool-call arguments a composer provider sent under one dialect's wire schema W",
    source_param="raw",
    suppresses=("R1", "R5"),
    invariant=(
        "raises ToolArgumentError (category wire_envelope) before use when set_pipeline arguments are not exactly "
        "one 'pipeline' object field, or a parameterless strict call lacks its exact true marker; "
        "other W failures are classified as wire_conformant only; removes a "
        "null only at a promoted position on openai_strict and never inserts, coerces or recurses into an "
        "undeclared key"
    ),
    test_ref="tests/unit/web/composer/test_wire_decode.py::test_decode_rejects_malformed_framing_and_preserves_null_semantics",
    test_fingerprint="daa6da2a616fe8cfabf8b9b1185ec35e56a8082e32836f63f6703982bba7dcf7",
)
def decode_wire_arguments(tool_name: str, dialect: ToolContractDialect, raw: dict[str, Any]) -> DecodedArguments:
    """Decode provider arguments sent under ``dialect`` into S's semantic form.

    1. ``wire_conformant``: whether ``raw`` validates against the W that was
       sent. Classification only.
    2. ``EnvelopeUnwrap``: set_pipeline arguments must be exactly
       ``{"pipeline": <object>}``, or :class:`ToolArgumentError` (category
       ``wire_envelope``) is raised.
    3. ``EmptyArgumentsMarker``: exactly the declared true marker decodes
       to ``{}``; missing, extra or different values raise ``wire_envelope``.
    4. ``StripNull`` (``openai_strict`` only): a ``null`` at a promoted
       position becomes an omitted key.

    Callers decode only a tool name that was in the list sent on that call;
    any other name raises :class:`WireProjectionError` (a caller bug).
    """
    tool = _sent_wire_tool(tool_name, dialect)
    if type(raw) is not dict:
        raise WireProjectionError("decode takes the JSON object the caller already decoded")
    wire_conformant = next(iter(_WIRE_VALIDATORS[dialect][tool_name].iter_errors(raw)), None) is None
    semantic: dict[str, Any] = deepcopy(raw)
    for node in tool.decode_plan:
        if type(node) is EmptyArgumentsMarker:
            if set(semantic) != {node.key} or semantic[node.key] is not True:
                raise ToolArgumentError(
                    argument=f"{tool_name} arguments",
                    expected="an object conforming to the declared argument schema",
                    actual_type="invalid_schema",
                    category=ToolArgumentErrorCategory.WIRE_ENVELOPE,
                )
            semantic = {}
        if type(node) is EnvelopeUnwrap:
            if set(semantic) != {node.key} or type(semantic[node.key]) is not dict:
                raise ToolArgumentError(
                    argument=f"{tool_name} arguments",
                    expected="an object conforming to the declared argument schema",
                    actual_type="invalid_schema",
                    category=ToolArgumentErrorCategory.WIRE_ENVELOPE,
                )
            semantic = semantic[node.key]
    if dialect == ToolContractDialect.OPENAI_STRICT:
        for node in tool.decode_plan:
            if type(node) is StripNull:
                _strip_null(semantic, node.path)
    return DecodedArguments(semantic=semantic, wire_conformant=wire_conformant)
''',
    "wire._sent_wire_tool": r"""def _sent_wire_tool(tool_name: str, dialect: ToolContractDialect) -> WireTool:
    tools = _WIRE_TOOL_DEFS[dialect]
    if tool_name not in tools:
        # A caller bug, not a model error: callers decode only the names in
        # the list sent on that call. The name is not echoed, because a
        # buggy caller could pass a model-authored one.
        raise WireProjectionError(f"no {dialect} wire projection for the requested tool name")
    return tools[tool_name]
""",
    "wire._strip_null": r'''def _strip_null(arguments: dict[str, Any], path: tuple[str, ...]) -> None:
    """Remove the key at ``path`` when it is present and ``null``; touch nothing else."""
    node: Any = arguments
    for key in path[:-1]:
        if type(node) is not dict or key not in node:
            return
        node = node[key]
    leaf = path[-1]
    if type(node) is dict and leaf in node and node[leaf] is None:
        del node[leaf]
''',
    "wire.DecodedArguments": r'''@dataclass(frozen=True, slots=True)
class DecodedArguments:
    """Provider arguments decoded back to the flat semantic form S admits.

    ``semantic`` is deep-frozen. ``wire_conformant`` records whether the raw
    arguments conformed to the W they were sent under; it is a
    classification, never an admission decision.
    """

    semantic: Mapping[str, Any]
    wire_conformant: bool

    def __post_init__(self) -> None:
        freeze_fields(self, "semantic")
''',
    "discovery.is_mutation_tool": r'''def is_mutation_tool(name: str) -> bool:
    """Return True when a composer tool can mutate session state or owned artifacts."""
    return name in _MUTATION_TOOL_NAMES or name in _BLOB_MUTATION_TOOL_NAMES or name in _SECRET_MUTATION_TOOL_NAMES
''',
    "discovery.is_cacheable_discovery_tool": r'''def is_cacheable_discovery_tool(name: str) -> bool:
    """Return True if the tool's results can be cached within a compose() call."""
    return name in _CACHEABLE_DISCOVERY_TOOL_NAMES
''',
    "hashing._normalize_frozen_and_reject_non_finite": r'''def _normalize_frozen_and_reject_non_finite(obj: Any) -> Any:
    """Normalize frozen containers and reject non-finite floats.

    Single recursive traversal that:
    - Converts any ``Mapping`` (including ``MappingProxyType``) → ``dict``
    - Converts ``tuple`` → ``list``
    - Rejects ``frozenset`` with ``TypeError`` (no canonical JSON ordering)
    - Rejects NaN/Infinity with ``ValueError``
    - Returns the normalized structure ready for ``rfc8785.dumps()``
    """
    if isinstance(obj, float):
        if math.isnan(obj):
            raise ValueError(f"Cannot canonicalize NaN. Use None for missing values, not NaN. Got: {obj!r}")
        if math.isinf(obj):
            raise ValueError(f"Cannot canonicalize Infinity. Use None for missing values, not Infinity. Got: {obj!r}")
        return obj
    if isinstance(obj, frozenset):
        # Type only, never the set's repr: its members can be row values, and
        # this text reaches audit records through every hashing seam.
        raise TypeError("frozenset is not JSON-serializable and has no canonical ordering. Use list or tuple for ordered collections.")
    # Mapping ABC covers both dict and MappingProxyType. The dict
    # comprehension normalizes MappingProxyType → dict for rfc8785.
    if isinstance(obj, Mapping):
        return {k: _normalize_frozen_and_reject_non_finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalize_frozen_and_reject_non_finite(item) for item in obj]
    return obj
''',
    "hashing.canonical_json": r'''def canonical_json(obj: Any) -> str:
    """Produce canonical JSON per RFC 8785/JCS.

    Handles JSON-safe primitives and their frozen equivalents
    (``MappingProxyType`` → ``dict``, ``tuple`` → ``list``).
    For data containing pandas/numpy types or PipelineRow, use
    ``elspeth.core.canonical.canonical_json()`` instead.

    Args:
        obj: JSON-safe data structure, optionally containing frozen containers

    Returns:
        Canonical JSON string (deterministic key order, no whitespace)

    Raises:
        ValueError: If data contains NaN or Infinity
        TypeError: If data contains frozenset or other non-serializable types
    """
    normalized = _normalize_frozen_and_reject_non_finite(obj)
    result: bytes = rfc8785.dumps(normalized)
    return result.decode("utf-8")


# The largest magnitude an integer may have in canonical JSON: rfc8785 refuses
# any int beyond it (IntegerDomainError), so it never writes one.
''',
    "hashing.stable_hash": r'''def stable_hash(obj: Any) -> str:
    """Compute SHA-256 hash of canonical JSON for primitive data.

    For data containing pandas/numpy types or PipelineRow, use
    ``elspeth.core.canonical.stable_hash()`` instead.

    Args:
        obj: JSON-safe data structure (no pandas/numpy types)

    Returns:
        SHA-256 hex digest of canonical JSON
    """
    canonical = canonical_json(obj)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
''',
    "freeze.deep_freeze": r'''def deep_freeze(value: Any) -> Any:
    """Recursively freeze mutable containers.

    Converts ``dict`` (and any ``Mapping``) → ``MappingProxyType`` and
    ``list`` → ``tuple``, recursing into values. Non-container types
    (str, int, float, bool, None, enum members, dataclass instances)
    are returned unchanged.

    This is the standard freeze function for ``__post_init__`` guards
    on frozen dataclasses throughout the contracts layer.

    Examples:
        >>> deep_freeze({"a": [1, {"b": 2}]})
        MappingProxyType({'a': (1, MappingProxyType({'b': 2}))})

        >>> deep_freeze([{"x": 1}, {"y": 2}])
        (MappingProxyType({'x': 1}), MappingProxyType({'y': 2}))

        >>> from types import MappingProxyType
        >>> already = MappingProxyType({"k": "v"})
        >>> deep_freeze(already) == already  # detached copy, same content
        True
    """
    if isinstance(value, dict):
        return MappingProxyType({k: deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(deep_freeze(item) for item in value)
    # MappingProxyType is a READ-ONLY VIEW, not a detached copy. The
    # underlying dict may still be mutable through other references.
    # Always create a fresh dict from the proxy's items to detach.
    if isinstance(value, MappingProxyType):
        return MappingProxyType({k: deep_freeze(v) for k, v in value.items()})
    if type(value) is FrozenJsonArray:
        frozen_array = FrozenJsonArray(deep_freeze(item) for item in value)
        if all(a is b for a, b in zip(frozen_array, value, strict=True)):
            return value
        return frozen_array
    if isinstance(value, tuple):
        frozen_tup = tuple(deep_freeze(item) for item in value)
        if all(a is b for a, b in zip(frozen_tup, value, strict=True)):
            return value
        return frozen_tup
    if isinstance(value, set):
        return frozenset(deep_freeze(item) for item in value)
    if isinstance(value, frozenset):
        # frozenset elements are unordered; recurse but can only detect
        # change by identity of the rebuilt set.
        frozen_fs = frozenset(deep_freeze(item) for item in value)
        if frozen_fs == value:
            return value
        return frozen_fs
    # Non-dict Mapping types (OrderedDict is a dict subclass so handled above,
    # but other Mapping implementations like custom read-only wrappers are not).
    # Convert to dict first, then freeze recursively.
    if isinstance(value, Mapping):
        return MappingProxyType({k: deep_freeze(v) for k, v in value.items()})
    # Scalars and opaque objects (str, int, float, bool, None, enums, dataclasses)
    return value
''',
    "freeze.freeze_fields": r'''def freeze_fields(instance: object, *field_names: str) -> None:
    """Freeze named container fields on a frozen dataclass instance.

    Applies deep_freeze() to each named field, with identity-preserving
    idempotency (skips object.__setattr__ when the field is already frozen).

    This is the standard utility for __post_init__ methods on frozen
    dataclasses. Use it instead of hand-rolling deep_freeze + setattr.

    Example:
        @dataclass(frozen=True, slots=True)
        class MyRecord:
            data: Mapping[str, Any]
            items: Sequence[str]

            def __post_init__(self) -> None:
                freeze_fields(self, "data", "items")

    Raises:
        AttributeError: If any field_name is not a declared dataclass field.
    """
    import dataclasses

    declared = {f.name for f in dataclasses.fields(instance)}  # type: ignore[arg-type]
    unknown = set(field_names) - declared
    if unknown:
        raise AttributeError(f"freeze_fields: {sorted(unknown)} not declared on {type(instance).__name__}. Declared: {sorted(declared)}")
    for name in field_names:
        value = getattr(instance, name)
        frozen = deep_freeze(value)
        if frozen is not value:
            object.__setattr__(instance, name, frozen)
''',
    "batch._await_started_tool_stage": r'''async def _await_started_tool_stage[T](
    awaitable: Awaitable[T],
    *,
    audit: DispatchAudit,
    recorder: BufferingRecorder,
    failure_observation: ContinuationDispatchFailureObservation | None = None,
) -> T:
    """Close a started dispatch when one unaudited async stage cancels."""
    try:
        return await awaitable
    except asyncio.CancelledError as cancellation:
        if failure_observation is None:
            recorder.record(finish_cancelled(audit, exc=cancellation))
        else:
            failure_observation.observe_primary(cancellation)
            try:
                recorder.record_with_failure_observation(finish_cancelled(audit, exc=cancellation), failure_observation)
            except BaseException as error:
                combined = failure_observation.owner.failure(error)
                if combined is error:
                    raise
                raise combined from combined.__cause__
        raise
''',
    "boundary.trust_boundary": r'''def trust_boundary(
    *,
    tier: Literal[3],
    source: str,
    source_param: str,
    suppresses: tuple[BoundaryRule, ...],
    invariant: str,
    test_ref: str | None = None,
    test_fingerprint: str | None = None,
    non_raising: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Mark a function as a Tier-3 external-data trust boundary.

    The decorator attaches a :class:`TrustBoundaryMetadata` record to the
    wrapped function as the ``__trust_boundary__`` attribute and otherwise
    returns a strict passthrough wrapper. No runtime validation is performed
    by the decorator itself; the metadata is consumed by static analysis.

    Args:
        tier: Trust tier. Must be ``3``. Other tiers raise :class:`TypeError`
            at decoration time — Tier-1 and Tier-2 must crash on anomaly, not
            suppress.
        source: Human-readable description of the external data source
            (e.g. ``"LLM tool-call arguments emitted by composer model"``).
        source_param: Name of the function parameter that carries the
            external data. Validated against the wrapped function's
            signature at decoration time; raises :class:`TypeError` if it
            is not a parameter name on the function.
        suppresses: Tuple of :data:`BoundaryRule` IDs that the static
            analyzer may suppress inside the function body, restricted
            to names derived from ``source_param``.
        invariant: Operator-facing description of what the function
            guarantees on malformed input (e.g. ``"raises
            ToolArgumentError on shape mismatch; never coerces silently"``).
        test_ref: Optional pytest nodeid of the test that should cover the
            malformed-input invariant. A separate CI gate enforces that this
            is present, points to a real test, and that the named test's own
            body contains a raising assertion that directly invokes the
            decorated function through ``source_param``.
        test_fingerprint: Optional canonical AST fingerprint of the referenced
            test body. The companion CI gate requires this whenever
            ``test_ref`` is present and reports drift if the nodeid still
            resolves but the test function was renamed, repurposed, or edited
            after review.
        non_raising: Declare that this boundary returns a sentinel
            (``None``/empty/result object) on malformed ``source_param`` input
            and never raises on it. Set this for optional-extraction,
            advisory, and convert-to-result boundaries whose contract is
            return-not-raise — a raising honesty test cannot exist for them.
            When ``True``, the ``trust_boundary.tests`` gate skips the
            raising-test requirement and instead mechanically verifies that no
            ``raise`` in the function body is control-dependent on a
            ``source_param``-derived guard (i.e. the boundary genuinely cannot
            raise on bad input). Mutually exclusive with ``test_ref`` /
            ``test_fingerprint`` — a boundary either raises-and-is-tested or is
            non-raising, never both. Passing both raises :class:`TypeError` at
            decoration time.

    Returns:
        A decorator that, when applied to a function, returns a wrapper
        with the same signature, return type, and :func:`functools.wraps`
        attributes (``__name__``, ``__qualname__``, ``__doc__``,
        ``__module__``, ``__wrapped__``, ``__dict__``), plus an additional
        ``__trust_boundary__`` attribute holding a frozen
        :class:`TrustBoundaryMetadata` record.

    Raises:
        TypeError: If ``tier`` is not ``3``, or if ``source_param`` does
            not name a parameter of the wrapped function, or if the function
            is already decorated with ``@trust_boundary``, or if
            ``non_raising=True`` is combined with ``test_ref`` /
            ``test_fingerprint``. These checks fire at decoration time
            (module import) rather than at call time.
    """
    if tier != 3:
        # Offensive: catch misuse at the decoration site. Tier-1 and Tier-2
        # have data-manifesto-mandated crash semantics; a suppression
        # decorator at those tiers is structurally wrong, not configurably
        # wrong, so we reject it at import rather than offering a flag.
        raise TypeError("@trust_boundary only applies to tier=3 trust boundaries; Tier-1 and Tier-2 must crash, not suppress.")

    if non_raising and (test_ref is not None or test_fingerprint is not None):
        # A non-raising boundary returns a sentinel on malformed input, so a
        # raising honesty test cannot exist for it; carrying a test_ref would
        # be a contradiction. Reject at the decoration site rather than letting
        # the static honesty gate be the only place the lie is caught.
        raise TypeError(
            "@trust_boundary(non_raising=True) is mutually exclusive with test_ref/test_fingerprint; "
            "a non-raising boundary returns a sentinel on malformed input and cannot have a raising test."
        )

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        existing_metadata = func.__dict__["__trust_boundary__"] if "__trust_boundary__" in func.__dict__ else None
        if existing_metadata is not None:
            raise TypeError(
                f"@trust_boundary cannot be stacked on {func.__qualname__}; "
                "a function may carry exactly one trust-boundary metadata record."
            )

        signature = inspect.signature(func)
        if source_param not in signature.parameters:
            raise TypeError(
                f"@trust_boundary(source_param={source_param!r}) does not "
                f"name a parameter of {func.__qualname__}; "
                f"signature parameters are "
                f"{tuple(signature.parameters)!r}."
            )

        metadata = TrustBoundaryMetadata(
            tier=tier,
            source=source,
            source_param=source_param,
            suppresses=suppresses,
            invariant=invariant,
            qualname=func.__qualname__,
            func=func,
            test_ref=test_ref,
            test_fingerprint=test_fingerprint,
            non_raising=non_raising,
        )

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            return func(*args, **kwargs)

        # Attach the metadata as a well-known dunder so the static analyzer
        # can locate it from an AST FunctionDef without having to import
        # and call the wrapper. ``functools.wraps`` copies ``__dict__``
        # contents from func; we set this on the wrapper after the copy,
        # so the attribute belongs to the wrapper and not (accidentally)
        # to the original function object.
        wrapper.__trust_boundary__ = metadata  # type: ignore[attr-defined]
        return wrapper

    return decorator
''',
    "wire_module.source": r'''"""The static per-dialect wire projection W of the composer tool registry.

The flat registry schema S (``_dispatch.get_tool_definitions()``) is the
single authority for what a tool accepts: MCP advertises it, the runtime
Draft 2020-12 gate enforces it, and the byte pins in
``test_tool_declarations.py`` hold it. W is what the web composer *sends* a
provider. It is derived from S once, at import, by one pure walk per
:class:`ToolContractDialect`, and is never hand-written:

* ``none`` is S unchanged, except that the web ``set_pipeline`` arguments
  disclose the runtime naming constraints and
  carry a required ``{"pipeline": <flat document>}`` envelope. LiteLLM's
  Anthropic and Bedrock adapters keep unions nested below a property but
  discard root-level ``oneOf``, so the flat set_pipeline schema would lose
  its source/sources choice on those routes. No tool carries a ``strict``
  key: this is the list every route sent before strict tool contracts.
* ``openai_strict`` projects each tool whose S has no free-form object (32
  of the 42) into an OpenAI strict-capable schema:

  - every S-optional property becomes required and nullable (``[T, "null"]``,
    or ``anyOf: [{enum}, {"type": "null"}]`` for an enum), and the decode
    plan gains a ``StripNull`` node for it, so decode turns the ``null`` a
    grammar-bound model must send back into the omission S expects;
  - keywords outside ``WIRE_KEYWORD_ALLOWLIST`` move to a ledger and are
    rendered as description text from a closed template map;
  - the 6 descriptions that tell the model to *omit* a now-required key are
    rewritten to say "pass null" (``_STRICT_DESCRIPTION_OVERRIDES``);
  - a parameterless tool carries exactly ``{"_elspeth_no_arguments": true}``
    on the wire. Its dedicated decode node validates this framing before
    returning the empty semantic object; MCP and NONE keep ``{}``.

  The 10 option-bearing tools keep their ``none`` parameters, and the wire
  list stamps them ``strict: false``.

This module owns the projection, the ledger, the limits check and the
faithfulness gate, all of which run at import, so a projection that is not
faithful to S stops web boot rather than reaching a provider.

It also owns the way back. :func:`decode_wire_arguments` classifies the
provider's arguments against the W that was sent (``wire_conformant``),
unwraps the set_pipeline envelope and the exact empty-argument marker, and on
``openai_strict`` turns a ``null`` at a promoted position back into the
omission S expects. Framing is validated before decoding; S stays the
semantic contract. :func:`encode_semantic_arguments` is the inverse framing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, cast

from jsonschema import Draft202012Validator

from elspeth.contracts.composer_audit import ToolArgumentErrorCategory
from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.tools._dispatch import _TOOL_SCHEMA_BY_NAME, WIRE_KEYWORD_ALLOWLIST, get_tool_definitions
from elspeth.web.composer.tools.schema_contract import canonical_set_pipeline_schema
from elspeth.web.composer.tools.strict_profile import StrictViolationKind, check_openai_strict

__all__ = [
    "OPENAI_STRICT_LIMITS",
    "DecodedArguments",
    "EmptyArgumentsMarker",
    "EnvelopeUnwrap",
    "LedgerEntry",
    "StripNull",
    "WireLimits",
    "WireLimitsReport",
    "WireProjectionError",
    "WireTool",
    "assert_wire_projection_faithful",
    "build_wire_tool_defs",
    "decode_wire_arguments",
    "encode_semantic_arguments",
    "loop_tool_count",
    "omission_instruction_matches",
    "project_tool",
    "strict_capable_tool_count",
    "wire_limits_report",
    "wire_tool_definitions",
]


class WireProjectionError(Exception):
    """The wire projection cannot be built faithfully from S.

    Raised at import (so web boot fails), or by a caller that asks for a
    projection that does not exist. Never raised for model-authored input.
    """


# ---------------------------------------------------------------- data model


@dataclass(frozen=True, slots=True)
class EnvelopeUnwrap:
    """Decode node: the wire arguments are ``{key: <semantic arguments>}``."""

    key: str = "pipeline"


@dataclass(frozen=True, slots=True)
class StripNull:
    """Decode node: a ``null`` at this property path means "omitted" in S."""

    path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EmptyArgumentsMarker:
    """A required true wire marker represents the one empty semantic object."""

    key: str = "_elspeth_no_arguments"


DecodeNode = EnvelopeUnwrap | StripNull | EmptyArgumentsMarker


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """A keyword removed from W, rendered as description text instead.

    ``path`` is the RFC 6901 pointer of the owning subschema inside the
    tool's ``parameters`` (``""`` is the root).
    """

    path: str
    keyword: str
    value: Any

    def __post_init__(self) -> None:
        freeze_fields(self, "value")


@dataclass(frozen=True, slots=True)
class WireTool:
    """One tool as it is sent under one dialect.

    ``function`` is the deep-frozen ``{name, description, parameters}``;
    :meth:`thawed_function` returns a fresh plain-JSON copy (lists for every
    array, key order kept) for the wire.
    """

    name: str
    dialect: ToolContractDialect
    function: Mapping[str, Any]
    strict_capable: bool
    decode_plan: tuple[DecodeNode, ...]
    ledger: tuple[LedgerEntry, ...]
    promoted_paths: frozenset[tuple[str, ...]]

    def __post_init__(self) -> None:
        freeze_fields(self, "function", "decode_plan", "ledger")

    def thawed_function(self) -> dict[str, Any]:
        return cast(dict[str, Any], deep_thaw(self.function))


@dataclass(frozen=True, slots=True)
class DecodedArguments:
    """Provider arguments decoded back to the flat semantic form S admits.

    ``semantic`` is deep-frozen. ``wire_conformant`` records whether the raw
    arguments conformed to the W they were sent under; it is a
    classification, never an admission decision.
    """

    semantic: Mapping[str, Any]
    wire_conformant: bool

    def __post_init__(self) -> None:
        freeze_fields(self, "semantic")


@dataclass(frozen=True, slots=True)
class WireLimits:
    """Schema-size limits a strict grammar imposes on one request."""

    max_properties: int
    max_depth: int
    max_characters: int
    max_enum_values: int


# OpenAI strict mode: 5000 object properties, 10 levels of nesting, 120,000
# characters of property names, enum values and const values, and 1000 enum
# values (master plan §2.2).
OPENAI_STRICT_LIMITS: Final[WireLimits] = WireLimits(
    max_properties=5000,
    max_depth=10,
    max_characters=120_000,
    max_enum_values=1000,
)


@dataclass(frozen=True, slots=True)
class WireLimitsReport:
    """Measured size of one dialect's whole tool list.

    Counted over every tool in the list, not only the strict-capable ones,
    so the report is an upper bound on what any strict request carries.
    ``max_depth`` counts object and array schema nodes on the deepest path
    (the root object is 1). ``total_characters`` is the length of every
    property name, enum value and const value (non-strings as compact JSON).
    """

    dialect: ToolContractDialect
    total_properties: int
    max_depth: int
    total_characters: int
    total_enum_values: int
    strict_tool_count: int


# ---------------------------------------------------------------- omission prose

# ``(tool, pointer into the function object) -> (exact old text, replacement)``.
# The pointer ``/description`` is the tool description. Each entry is live:
# a missing old text raises (a stale override), and so does an entry for a
# tool that is not strict-capable. ``none`` keeps today's text.
_STRICT_DESCRIPTION_OVERRIDES: Final[Mapping[tuple[str, str], tuple[str, str]]] = MappingProxyType(
    {
        ("get_pipeline_state", "/parameters/properties/component/description"): (
            "Accepted full-state aliases: omit component,",
            "Accepted full-state aliases: pass null for component,",
        ),
        ("get_plugin_assistance", "/description"): (
            "Omit ``issue_code`` (or pass null) to get discovery-time guidance",
            "Pass null for ``issue_code`` to get discovery-time guidance",
        ),
        ("get_plugin_assistance", "/parameters/properties/issue_code/description"): (
            "Omit or pass null for discovery-time guidance.",
            "Pass null for discovery-time guidance.",
        ),
        ("list_models", "/parameters/properties/provider/description"): (
            "Omit to get a provider summary",
            "Pass null to get a provider summary",
        ),
        ("request_interpretation_review", "/parameters/properties/kind/description"): (
            "and OMIT llm_draft — the server computes",
            "and pass null for llm_draft — the server computes",
        ),
        ("request_interpretation_review", "/parameters/properties/llm_draft/description"): (
            "OMIT this when the review site already carries",
            "Pass null for llm_draft when the review site already carries",
        ),
    }
)

# The closed vocabulary of omission instructions the faithfulness gate
# rejects in a strict-capable W. It is a closed list: a phrasing outside it
# (for example "a full-state read (no component ...)") is not caught.
_OMISSION_VOCABULARY: Final[re.Pattern[str]] = re.compile(
    r"\bomit"
    r"|\bleave (it |this )?(out|blank|empty|unset)"
    r"|\bif not (provided|given|set|supplied)"
    r"|\bnot provided\b"
    r"|\b(if|when) absent\b"
    r"|\bdo not (send|pass|include|provide)\b",
    re.IGNORECASE,
)

# Reviewed exceptions: ``(tool, pointer into the function object) -> exact
# text``. The get_pipeline_state sentence is about the set_pipeline
# envelope, not about a promoted position. Each entry must be live.
_OMISSION_VOCABULARY_EXCEPTIONS: Final[Mapping[tuple[str, str], str]] = MappingProxyType(
    {
        ("get_pipeline_state", "/description"): "do not send its source/nodes/edges/outputs fields at the top level",
    }
)


def omission_instruction_matches(text: str) -> bool:
    """Return whether ``text`` contains an instruction to omit a key."""
    return _OMISSION_VOCABULARY.search(text) is not None


# ---------------------------------------------------------------- projection

# The one tool whose web arguments carry a provider envelope.
_ENVELOPE_TOOL: Final[str] = "set_pipeline"
_ENVELOPE_KEY: Final[str] = "pipeline"


def _envelope(parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {_ENVELOPE_KEY: parameters},
        "required": [_ENVELOPE_KEY],
        "additionalProperties": False,
    }


def _pointer(parent: str, key: str) -> str:
    return f"{parent}/{key.replace('~', '~0').replace('/', '~1')}"


def _length_bound(tool: str, pointer: str, keyword: str, value: Any) -> int:
    if type(value) is not int:
        raise WireProjectionError(f"{tool}{pointer}: {keyword} must be an integer to render into the ledger")
    return value


def _characters(count: int) -> str:
    return "character" if count == 1 else "characters"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _nullable(tool: str, pointer: str, node: dict[str, Any]) -> dict[str, Any]:
    """Return ``node`` widened to also accept ``null`` (projection rule 3)."""
    if "const" in node:
        raise WireProjectionError(f"{tool}{pointer}: no nullable rule for an optional const")
    if "enum" in node:
        member: dict[str, Any] = {"type": node["type"]} if "type" in node else {}
        member["enum"] = node["enum"]
        widened: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("type", "enum"):
                if "anyOf" not in widened:
                    widened["anyOf"] = [member, {"type": "null"}]
            elif key == "description":
                widened[key] = value
            else:
                raise WireProjectionError(f"{tool}{pointer}: no nullable rule for an optional enum carrying {key!r}")
        return widened
    if "type" in node:
        declared = node["type"]
        if type(declared) is str:
            nullable_type: Any = [declared] if declared == "null" else [declared, "null"]
        elif type(declared) is list:
            nullable_type = declared if "null" in declared else [*declared, "null"]
        else:
            raise WireProjectionError(f"{tool}{pointer}: type must be a string or a list")
        return {key: (nullable_type if key == "type" else value) for key, value in node.items()}
    raise WireProjectionError(f"{tool}{pointer}: no nullable rule for an optional subschema without a type")


class _StrictProjection:
    """One ``openai_strict`` walk over one strict-capable tool's flat S."""

    def __init__(self, tool: str) -> None:
        self.tool = tool
        self.ledger: list[LedgerEntry] = []
        self.strip: list[StripNull] = []
        self.tool_sentences: list[str] = []

    def _ledger_sentence(
        self,
        node: dict[str, Any],
        pointer: str,
        keyword: str,
        value: Any,
        *,
        promoted: bool,
        is_items: bool,
    ) -> tuple[str | None, str | None]:
        """Return ``(own sentence, sentence for the parent array)`` for one ledgered keyword."""
        has_description = "description" in node
        if keyword == "maxLength":
            bound = _length_bound(self.tool, pointer, keyword, value)
            if has_description:
                return f"At most {bound} {_characters(bound)}.", None
            if is_items:
                return None, f"Each item has at most {bound} {_characters(bound)}."
        elif keyword == "minLength":
            bound = _length_bound(self.tool, pointer, keyword, value)
            if has_description:
                return f"At least {bound} {_characters(bound)}.", None
        elif keyword == "default":
            if promoted and has_description:
                return f"Pass null to use the default ({_canonical_json(value)}).", None
        elif keyword == "examples" and pointer == "":
            if type(value) is not list or not value:
                raise WireProjectionError(f"{self.tool}: root examples must be a non-empty list")
            self.tool_sentences.extend(f"Example arguments: {_canonical_json(example)}." for example in value)
            return None, None
        raise WireProjectionError(f"{self.tool}{pointer}: no wire rule for keyword {keyword!r} on this subschema")

    def project(
        self,
        node: dict[str, Any],
        pointer: str,
        property_path: tuple[str, ...] | None,
        *,
        promoted: bool,
        is_items: bool,
    ) -> tuple[dict[str, Any], list[str]]:
        """Project one subschema; return it and any sentences its parent array owns."""
        if type(node) is not dict:
            raise WireProjectionError(f"{self.tool}{pointer}: subschema is not an object")
        projected: dict[str, Any] = {}
        own_sentences: list[str] = []
        parent_sentences: list[str] = []
        item_sentences: list[str] = []
        promoted_names: list[str] = []
        for key, value in node.items():
            key_pointer = _pointer(pointer, key)
            if key not in WIRE_KEYWORD_ALLOWLIST:
                self.ledger.append(LedgerEntry(path=pointer, keyword=key, value=deepcopy(value)))
                own, for_parent = self._ledger_sentence(node, pointer, key, value, promoted=promoted, is_items=is_items)
                if own is not None:
                    own_sentences.append(own)
                if for_parent is not None:
                    parent_sentences.append(for_parent)
                continue
            if key == "properties":
                if type(value) is not dict:
                    raise WireProjectionError(f"{self.tool}{key_pointer}: properties must be an object")
                required = node["required"] if "required" in node else []
                children: dict[str, Any] = {}
                for name, child in value.items():
                    child_promoted = name not in required
                    child_path = None if property_path is None else (*property_path, name)
                    child_pointer = _pointer(key_pointer, name)
                    projected_child, _ = self.project(child, child_pointer, child_path, promoted=child_promoted, is_items=False)
                    if child_promoted:
                        if child_path is None:
                            raise WireProjectionError(
                                f"{self.tool}{child_pointer}: no decode rule for an optional property inside an array"
                            )
                        projected_child = _nullable(self.tool, child_pointer, projected_child)
                        self.strip.append(StripNull(path=child_path))
                        promoted_names.append(name)
                    children[name] = projected_child
                projected[key] = children
            elif key == "required":
                if type(value) is not list or "properties" not in node:
                    raise WireProjectionError(f"{self.tool}{key_pointer}: required needs a properties map")
                projected[key] = [*value, *(name for name in node["properties"] if name not in value)]
            elif key == "items":
                projected_items, item_sentences = self.project(value, key_pointer, None, promoted=False, is_items=True)
                projected[key] = projected_items
            elif key == "anyOf":
                if type(value) is not list:
                    raise WireProjectionError(f"{self.tool}{key_pointer}: anyOf must be a list")
                projected[key] = [
                    self.project(member, _pointer(key_pointer, str(index)), None, promoted=False, is_items=False)[0]
                    for index, member in enumerate(value)
                ]
            elif key == "additionalProperties":
                if value is not False:
                    raise WireProjectionError(f"{self.tool}{key_pointer}: a strict-capable object must be closed")
                projected[key] = False
            else:
                projected[key] = deepcopy(value)
        if promoted_names and "required" not in node:
            projected["required"] = promoted_names
        own_sentences.extend(item_sentences)
        if own_sentences:
            if "description" not in projected or type(projected["description"]) is not str:
                raise WireProjectionError(f"{self.tool}{pointer}: ledger text has no description to render into")
            projected["description"] = " ".join([projected["description"], *own_sentences])
        return projected, parent_sentences


def _resolve_description(function: dict[str, Any], tool: str, pointer: str) -> tuple[dict[str, Any], str]:
    """Return the container and key of the description string at ``pointer``."""
    tokens = pointer.lstrip("/").split("/")
    container: Any = function
    for token in tokens[:-1]:
        if type(container) is not dict or token not in container:
            raise WireProjectionError(f"{tool}{pointer}: override pointer does not resolve")
        container = container[token]
    key = tokens[-1]
    if type(container) is not dict or key not in container or type(container[key]) is not str:
        raise WireProjectionError(f"{tool}{pointer}: override pointer does not name a description")
    return container, key


def _apply_overrides(
    function: dict[str, Any],
    tool: str,
    overrides: Mapping[tuple[str, str], tuple[str, str]],
) -> None:
    for (override_tool, pointer), (old, new) in overrides.items():
        if override_tool != tool:
            continue
        container, key = _resolve_description(function, tool, pointer)
        if container[key].count(old) != 1:
            raise WireProjectionError(f"{tool}{pointer}: stale description override (old text not found exactly once)")
        container[key] = container[key].replace(old, new)


def project_tool(
    definition: Mapping[str, Any],
    dialect: ToolContractDialect,
    *,
    overrides: Mapping[tuple[str, str], tuple[str, str]] = _STRICT_DESCRIPTION_OVERRIDES,
) -> WireTool:
    """Project one flat registry definition onto the wire of ``dialect``."""
    name = definition["name"]
    flat = deepcopy(definition["parameters"])
    if type(flat) is not dict:
        raise WireProjectionError(f"{name}: parameters must be an object")
    decode_plan: tuple[DecodeNode, ...] = (EnvelopeUnwrap(key=_ENVELOPE_KEY),) if name == _ENVELOPE_TOOL else ()
    none_parameters = _envelope(flat) if name == _ENVELOPE_TOOL else flat
    free_form = any(row.kind is StrictViolationKind.FREE_FORM_OBJECT for row in check_openai_strict(flat, tool=name))
    if dialect == ToolContractDialect.NONE or free_form:
        return WireTool(
            name=name,
            dialect=dialect,
            function={"name": name, "description": definition["description"], "parameters": none_parameters},
            strict_capable=False,
            decode_plan=decode_plan,
            ledger=(),
            promoted_paths=frozenset(),
        )
    if name == _ENVELOPE_TOOL:
        raise WireProjectionError(f"{name}: no strict projection rule for an enveloped tool")
    projection = _StrictProjection(name)
    parameters, orphaned = projection.project(flat, "", (), promoted=False, is_items=False)
    if orphaned:
        raise WireProjectionError(f"{name}: ledger text has no description to render into")
    description = " ".join([definition["description"], *projection.tool_sentences])
    marker: EmptyArgumentsMarker | None = None
    if "properties" in flat and flat["properties"] == {}:
        # The deployed forwarding route cannot generate an empty object
        # reliably, even with strict false or the per-tool stamp omitted.
        # This closed wire-only marker gives its grammar a token to emit;
        # decode admits exactly this framing and the semantic tool stays {}.
        marker = EmptyArgumentsMarker()
        parameters["properties"] = {marker.key: {"type": "boolean", "enum": [True]}}
        parameters["required"] = [marker.key]
        description += ' This tool takes no semantic arguments. Call it with {"_elspeth_no_arguments": true} exactly.'
    function = {"name": name, "description": description, "parameters": parameters}
    _apply_overrides(function, name, overrides)
    rows = check_openai_strict(parameters, tool=name)
    if rows:
        found = ", ".join(f"{row.path or '/'} {row.kind}" for row in rows)
        raise WireProjectionError(f"{name}: projected schema is not strict-capable: {found}")
    return WireTool(
        name=name,
        dialect=dialect,
        function=function,
        strict_capable=True,
        decode_plan=(marker,) if marker is not None else tuple(projection.strip),
        ledger=tuple(projection.ledger),
        promoted_paths=frozenset(node.path for node in projection.strip),
    )


# ---------------------------------------------------------------- faithfulness


def _is_nullable(node: Any) -> bool:
    if type(node) is not dict:
        return False
    if "type" in node:
        declared = node["type"]
        if declared == "null" or (type(declared) is list and "null" in declared):
            return True
    if "enum" in node and type(node["enum"]) is list and None in node["enum"]:
        return True
    if "anyOf" in node and type(node["anyOf"]) is list:
        return any(_is_nullable(member) for member in node["anyOf"])
    return False


def _requiredness_failures(
    tool: WireTool,
    flat: Any,
    wire: Any,
    path: tuple[str, ...],
) -> Iterator[str]:
    """Yield every property whose wire requiredness is not faithful to S (checks 2 and 3)."""
    if type(flat) is not dict or type(wire) is not dict or "properties" not in flat:
        return
    flat_required = flat["required"] if "required" in flat else []
    wire_required = wire["required"] if "required" in wire else []
    for name, flat_child in flat["properties"].items():
        child_path = (*path, name)
        wire_child = wire["properties"][name] if "properties" in wire and name in wire["properties"] else None
        if wire_child is None:
            yield f"{tool.name}: {'.'.join(child_path)} is missing from W"
            continue
        if name not in wire_required:
            yield f"{tool.name}: {'.'.join(child_path)} is not wire-required"
        if name in flat_required:
            if _is_nullable(wire_child) is not _is_nullable(flat_child):
                yield f"{tool.name}: flat-required {'.'.join(child_path)} changed nullability on the wire"
            if child_path in tool.promoted_paths:
                yield f"{tool.name}: flat-required {'.'.join(child_path)} has a StripNull node"
        else:
            if not _is_nullable(wire_child):
                yield f"{tool.name}: promoted {'.'.join(child_path)} is not nullable on the wire"
            if child_path not in tool.promoted_paths:
                yield f"{tool.name}: promoted {'.'.join(child_path)} has no StripNull node"
        yield from _requiredness_failures(tool, flat_child, wire_child, child_path)


def _flat_optional_paths(flat: Any, path: tuple[str, ...]) -> Iterator[tuple[str, ...]]:
    if type(flat) is not dict or "properties" not in flat:
        return
    required = flat["required"] if "required" in flat else []
    for name, child in flat["properties"].items():
        if name not in required:
            yield (*path, name)
        yield from _flat_optional_paths(child, (*path, name))


def _description_sites(value: Any, pointer: str) -> Iterator[tuple[str, str]]:
    """Yield ``(pointer, text)`` for every description string below ``value``."""
    if type(value) is dict:
        for key, child in value.items():
            child_pointer = _pointer(pointer, key)
            if key == "description" and type(child) is str:
                yield child_pointer, child
            else:
                yield from _description_sites(child, child_pointer)
    elif type(value) is list:
        for index, child in enumerate(value):
            yield from _description_sites(child, _pointer(pointer, str(index)))


def _null_enum_found(value: Any) -> bool:
    if type(value) is dict:
        if "enum" in value and type(value["enum"]) is list and None in value["enum"]:
            return True
        return any(_null_enum_found(child) for child in value.values())
    if type(value) is list:
        return any(_null_enum_found(child) for child in value)
    return False


def _same_json(left: Any, right: Any) -> bool:
    """Type-sensitive, key-order-sensitive JSON equality."""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return list(left) == list(right) and all(_same_json(left[key], right[key]) for key in left)
    if type(left) is list:
        return len(left) == len(right) and all(_same_json(a, b) for a, b in zip(left, right, strict=True))
    return bool(left == right)


def assert_wire_projection_faithful(
    defs: Mapping[ToolContractDialect, Mapping[str, WireTool]],
    definitions: Sequence[Mapping[str, Any]],
    *,
    omission_exceptions: Mapping[tuple[str, str], str] = _OMISSION_VOCABULARY_EXCEPTIONS,
) -> None:
    """Raise :class:`WireProjectionError` unless every W is faithful to its S."""
    flat_by_name = {definition["name"]: definition for definition in definitions}
    strict_defs = defs[ToolContractDialect.OPENAI_STRICT]
    used_exceptions: set[tuple[str, str]] = set()
    for name, tool in strict_defs.items():
        function = tool.thawed_function()
        flat = flat_by_name[name]["parameters"]
        for entry in tool.ledger:
            # Check 4: a ledgered keyword is only safe when S still enforces it.
            if name not in _TOOL_SCHEMA_BY_NAME:
                raise WireProjectionError(f"{name}: ledgered {entry.keyword!r} sits on a tool the S gate does not cover")
        if not tool.strict_capable:
            continue
        # Check 1: strict-capable W passes the strict checker.
        rows = check_openai_strict(function["parameters"], tool=name)
        if rows:
            raise WireProjectionError(f"{name}: strict-capable W fails the strict checker")
        marker_nodes = tuple(node for node in tool.decode_plan if type(node) is EmptyArgumentsMarker)
        parameterless = "properties" in flat and flat["properties"] == {}
        if parameterless:
            marker = EmptyArgumentsMarker()
            expected_parameters = deepcopy(flat)
            expected_parameters["properties"] = {marker.key: {"type": "boolean", "enum": [True]}}
            expected_parameters["required"] = [marker.key]
            if tool.decode_plan != (marker,) or not _same_json(function["parameters"], expected_parameters):
                raise WireProjectionError(f"{name}: empty-argument marker does not match its semantic contract")
        elif marker_nodes:
            raise WireProjectionError(f"{name}: empty-argument marker requires a parameterless semantic contract")
        # Checks 2 and 3: requiredness and nullability follow S.
        failures = list(_requiredness_failures(tool, flat, function["parameters"], ()))
        if failures:
            raise WireProjectionError("; ".join(failures))
        if tool.promoted_paths != frozenset(_flat_optional_paths(flat, ())):
            raise WireProjectionError(f"{name}: StripNull nodes do not match the S-optional properties")
        # Check 5: no omission instruction anywhere in a strict-capable W.
        sites = [("/description", function["description"]), *_description_sites(function["parameters"], "/parameters")]
        for pointer, text in sites:
            exception = omission_exceptions[(name, pointer)] if (name, pointer) in omission_exceptions else None
            if exception is not None:
                if exception not in text:
                    raise WireProjectionError(f"{name}{pointer}: stale omission-vocabulary exception")
                used_exceptions.add((name, pointer))
                text = text.replace(exception, "")
            if omission_instruction_matches(text):
                raise WireProjectionError(f"{name}{pointer}: description instructs the model to omit a wire-required key")
        # Check 6: no enum in a strict-capable W contains null.
        if _null_enum_found(function["parameters"]):
            raise WireProjectionError(f"{name}: an enum in the strict-capable W contains null")
    stale = set(omission_exceptions) - used_exceptions
    if stale:
        raise WireProjectionError(f"stale omission-vocabulary exceptions: {sorted(stale)}")
    # Check 7: the none W, as thawed for the wire, is S plus the envelope.
    none_defs = defs[ToolContractDialect.NONE]
    if list(none_defs) != list(flat_by_name):
        raise WireProjectionError("none W does not list the registry tools in registry order")
    for name, tool in none_defs.items():
        definition = flat_by_name[name]
        parameters = definition["parameters"]
        expected = {
            "name": name,
            "description": definition["description"],
            "parameters": _envelope(parameters) if name == _ENVELOPE_TOOL else parameters,
        }
        if not _same_json(tool.thawed_function(), expected):
            raise WireProjectionError(f"{name}: none W is not S")


# ---------------------------------------------------------------- limits


def _subschemas(node: dict[str, Any]) -> Iterator[Any]:
    for key, value in node.items():
        if key == "properties" and type(value) is dict:
            yield from value.values()
        elif key in ("items", "additionalProperties", "not") and type(value) is dict:
            yield value
        elif key in ("anyOf", "oneOf", "allOf") and type(value) is list:
            yield from value


def _is_container_schema(node: dict[str, Any]) -> bool:
    if "type" not in node:
        return "properties" in node
    declared = node["type"]
    names = declared if type(declared) is list else [declared]
    return "object" in names or "array" in names


def _measure(node: Any, depth: int, totals: dict[str, int]) -> None:
    if type(node) is not dict:
        return
    here = depth + (1 if _is_container_schema(node) else 0)
    totals["max_depth"] = max(totals["max_depth"], here)
    if "properties" in node and type(node["properties"]) is dict:
        totals["total_properties"] += len(node["properties"])
        totals["total_characters"] += sum(len(name) for name in node["properties"])
    for keyword in ("enum", "const"):
        if keyword in node:
            values = node[keyword] if keyword == "enum" and type(node[keyword]) is list else [node[keyword]]
            if keyword == "enum":
                totals["total_enum_values"] += len(values)
            totals["total_characters"] += sum(len(value) if type(value) is str else len(_canonical_json(value)) for value in values)
    for child in _subschemas(node):
        _measure(child, here, totals)


def _limits_report(dialect: ToolContractDialect, tools: Mapping[str, WireTool]) -> WireLimitsReport:
    totals = {"total_properties": 0, "max_depth": 0, "total_characters": 0, "total_enum_values": 0}
    for tool in tools.values():
        _measure(tool.thawed_function()["parameters"], 0, totals)
    return WireLimitsReport(
        dialect=dialect,
        total_properties=totals["total_properties"],
        max_depth=totals["max_depth"],
        total_characters=totals["total_characters"],
        total_enum_values=totals["total_enum_values"],
        strict_tool_count=sum(1 for tool in tools.values() if tool.strict_capable),
    )


def _check_limits(report: WireLimitsReport, limits: WireLimits) -> None:
    exceeded = [
        f"{label} {measured} > {limit}"
        for label, measured, limit in (
            ("properties", report.total_properties, limits.max_properties),
            ("depth", report.max_depth, limits.max_depth),
            ("characters", report.total_characters, limits.max_characters),
            ("enum values", report.total_enum_values, limits.max_enum_values),
        )
        if measured > limit
    ]
    if exceeded:
        raise WireProjectionError(f"{report.dialect} tool list exceeds the strict limits: {', '.join(exceeded)}")


# ---------------------------------------------------------------- build


def build_wire_tool_defs(
    definitions: Sequence[Mapping[str, Any]],
    *,
    limits: WireLimits,
    overrides: Mapping[tuple[str, str], tuple[str, str]] = _STRICT_DESCRIPTION_OVERRIDES,
    omission_exceptions: Mapping[tuple[str, str], str] = _OMISSION_VOCABULARY_EXCEPTIONS,
) -> Mapping[ToolContractDialect, Mapping[str, WireTool]]:
    """Project, check and freeze every dialect's W for ``definitions``.

    Pure: the only inputs are the arguments. Raises
    :class:`WireProjectionError` on any unfaithful projection, stale
    override or exception, or exceeded limit.
    """
    names = [definition["name"] for definition in definitions]
    if len(set(names)) != len(names):
        raise WireProjectionError("tool definitions contain a duplicate name")
    defs: dict[ToolContractDialect, Mapping[str, WireTool]] = {
        dialect: MappingProxyType(
            {definition["name"]: project_tool(definition, dialect, overrides=overrides) for definition in definitions}
        )
        for dialect in ToolContractDialect
    }
    strict_capable = {name for name, tool in defs[ToolContractDialect.OPENAI_STRICT].items() if tool.strict_capable}
    stale = sorted(key for key in overrides if key[0] not in strict_capable)
    if stale:
        raise WireProjectionError(f"description overrides target tools that are not strict-capable: {stale}")
    assert_wire_projection_faithful(defs, definitions, omission_exceptions=omission_exceptions)
    for dialect, tools in defs.items():
        _check_limits(_limits_report(dialect, tools), limits)
    return MappingProxyType(defs)


def _build_loop_wire_tool_defs() -> Mapping[ToolContractDialect, Mapping[str, WireTool]]:
    """Build owned wire contracts from the naming-disclosed registry copy.

    Projection, size checks and wire-conformance validators must consume this
    same document. Adding disclosures only after projection would leave the
    decoder classifying against a different schema from the one sent.
    """
    definitions = get_tool_definitions()
    for definition in definitions:
        if definition["name"] == "set_pipeline":
            definition["parameters"] = canonical_set_pipeline_schema()
            return build_wire_tool_defs(definitions, limits=OPENAI_STRICT_LIMITS)
    raise WireProjectionError("Compose loop must advertise set_pipeline")


# Built once, at import, in registry order (``wire_secret_ref`` last).
_WIRE_TOOL_DEFS: Final[Mapping[ToolContractDialect, Mapping[str, WireTool]]] = _build_loop_wire_tool_defs()

_WIRE_LIMITS_REPORTS: Final[Mapping[ToolContractDialect, WireLimitsReport]] = MappingProxyType(
    {dialect: _limits_report(dialect, tools) for dialect, tools in _WIRE_TOOL_DEFS.items()}
)


def wire_limits_report(dialect: ToolContractDialect) -> WireLimitsReport:
    """Return the measured size of ``dialect``'s whole tool list."""
    return _WIRE_LIMITS_REPORTS[dialect]


def strict_capable_tool_count() -> int:
    """How many loop tools the ``openai_strict`` projection marks strict-capable.

    A property of the tool set (32 of 42 when S1 landed), not of any route,
    setting or sent list, so ``/api/system/status`` may publish it (D14).
    """
    return sum(1 for tool in _WIRE_TOOL_DEFS[ToolContractDialect.OPENAI_STRICT].values() if tool.strict_capable)


def loop_tool_count() -> int:
    """How many tools the compose loop's list carries (a property of the tool set)."""
    return len(_WIRE_TOOL_DEFS[ToolContractDialect.NONE])


def wire_tool_definitions(dialect: ToolContractDialect) -> list[dict[str, Any]]:
    """Return fresh LiteLLM function dicts for ``dialect``, in registry order.

    ``none``: ``{"type": "function", "function": {name, description,
    parameters}}`` with no ``strict`` key. ``openai_strict``: the same plus
    ``function.strict``, ``True`` on the strict-capable tools and an explicit
    ``False`` on the others.
    """
    tools: list[dict[str, Any]] = []
    for tool in _WIRE_TOOL_DEFS[dialect].values():
        function = tool.thawed_function()
        if dialect == ToolContractDialect.OPENAI_STRICT:
            function["strict"] = tool.strict_capable
        tools.append({"type": "function", "function": function})
    return tools


# ---------------------------------------------------------------- decode / encode

# One compiled validator per sent W, so ``wire_conformant`` holds the raw
# arguments to exactly the schema the provider was given.
_WIRE_VALIDATORS: Final[Mapping[ToolContractDialect, Mapping[str, Draft202012Validator]]] = MappingProxyType(
    {
        dialect: MappingProxyType({name: Draft202012Validator(tool.thawed_function()["parameters"]) for name, tool in tools.items()})
        for dialect, tools in _WIRE_TOOL_DEFS.items()
    }
)


def _sent_wire_tool(tool_name: str, dialect: ToolContractDialect) -> WireTool:
    tools = _WIRE_TOOL_DEFS[dialect]
    if tool_name not in tools:
        # A caller bug, not a model error: callers decode only the names in
        # the list sent on that call. The name is not echoed, because a
        # buggy caller could pass a model-authored one.
        raise WireProjectionError(f"no {dialect} wire projection for the requested tool name")
    return tools[tool_name]


def _strip_null(arguments: dict[str, Any], path: tuple[str, ...]) -> None:
    """Remove the key at ``path`` when it is present and ``null``; touch nothing else."""
    node: Any = arguments
    for key in path[:-1]:
        if type(node) is not dict or key not in node:
            return
        node = node[key]
    leaf = path[-1]
    if type(node) is dict and leaf in node and node[leaf] is None:
        del node[leaf]


@trust_boundary(
    tier=3,
    source="the JSON-decoded tool-call arguments a composer provider sent under one dialect's wire schema W",
    source_param="raw",
    suppresses=("R1", "R5"),
    invariant=(
        "raises ToolArgumentError (category wire_envelope) before use when set_pipeline arguments are not exactly "
        "one 'pipeline' object field, or a parameterless strict call lacks its exact true marker; "
        "other W failures are classified as wire_conformant only; removes a "
        "null only at a promoted position on openai_strict and never inserts, coerces or recurses into an "
        "undeclared key"
    ),
    test_ref="tests/unit/web/composer/test_wire_decode.py::test_decode_rejects_malformed_framing_and_preserves_null_semantics",
    test_fingerprint="daa6da2a616fe8cfabf8b9b1185ec35e56a8082e32836f63f6703982bba7dcf7",
)
def decode_wire_arguments(tool_name: str, dialect: ToolContractDialect, raw: dict[str, Any]) -> DecodedArguments:
    """Decode provider arguments sent under ``dialect`` into S's semantic form.

    1. ``wire_conformant``: whether ``raw`` validates against the W that was
       sent. Classification only.
    2. ``EnvelopeUnwrap``: set_pipeline arguments must be exactly
       ``{"pipeline": <object>}``, or :class:`ToolArgumentError` (category
       ``wire_envelope``) is raised.
    3. ``EmptyArgumentsMarker``: exactly the declared true marker decodes
       to ``{}``; missing, extra or different values raise ``wire_envelope``.
    4. ``StripNull`` (``openai_strict`` only): a ``null`` at a promoted
       position becomes an omitted key.

    Callers decode only a tool name that was in the list sent on that call;
    any other name raises :class:`WireProjectionError` (a caller bug).
    """
    tool = _sent_wire_tool(tool_name, dialect)
    if type(raw) is not dict:
        raise WireProjectionError("decode takes the JSON object the caller already decoded")
    wire_conformant = next(iter(_WIRE_VALIDATORS[dialect][tool_name].iter_errors(raw)), None) is None
    semantic: dict[str, Any] = deepcopy(raw)
    for node in tool.decode_plan:
        if type(node) is EmptyArgumentsMarker:
            if set(semantic) != {node.key} or semantic[node.key] is not True:
                raise ToolArgumentError(
                    argument=f"{tool_name} arguments",
                    expected="an object conforming to the declared argument schema",
                    actual_type="invalid_schema",
                    category=ToolArgumentErrorCategory.WIRE_ENVELOPE,
                )
            semantic = {}
        if type(node) is EnvelopeUnwrap:
            if set(semantic) != {node.key} or type(semantic[node.key]) is not dict:
                raise ToolArgumentError(
                    argument=f"{tool_name} arguments",
                    expected="an object conforming to the declared argument schema",
                    actual_type="invalid_schema",
                    category=ToolArgumentErrorCategory.WIRE_ENVELOPE,
                )
            semantic = semantic[node.key]
    if dialect == ToolContractDialect.OPENAI_STRICT:
        for node in tool.decode_plan:
            if type(node) is StripNull:
                _strip_null(semantic, node.path)
    return DecodedArguments(semantic=semantic, wire_conformant=wire_conformant)


def encode_semantic_arguments(tool_name: str, dialect: ToolContractDialect, semantic: Mapping[str, Any]) -> dict[str, Any]:
    """Return the wire framing of semantic arguments.

    set_pipeline becomes ``{"pipeline": semantic}``; strict parameterless
    tools emit their true marker. Other tools return a fresh unchanged copy.
    """
    tool = _sent_wire_tool(tool_name, dialect)
    arguments = cast(dict[str, Any], deep_thaw(semantic))
    for node in tool.decode_plan:
        if type(node) is EmptyArgumentsMarker:
            if arguments:
                raise WireProjectionError("parameterless wire encoding requires empty semantic arguments")
            return {node.key: True}
        if type(node) is EnvelopeUnwrap:
            return {node.key: arguments}
    return arguments
''',
    "wire.WireTool": r'''@dataclass(frozen=True, slots=True)
class WireTool:
    """One tool as it is sent under one dialect.

    ``function`` is the deep-frozen ``{name, description, parameters}``;
    :meth:`thawed_function` returns a fresh plain-JSON copy (lists for every
    array, key order kept) for the wire.
    """

    name: str
    dialect: ToolContractDialect
    function: Mapping[str, Any]
    strict_capable: bool
    decode_plan: tuple[DecodeNode, ...]
    ledger: tuple[LedgerEntry, ...]
    promoted_paths: frozenset[tuple[str, ...]]

    def __post_init__(self) -> None:
        freeze_fields(self, "function", "decode_plan", "ledger")

    def thawed_function(self) -> dict[str, Any]:
        return cast(dict[str, Any], deep_thaw(self.function))
''',
    "wire.EnvelopeUnwrap": r'''@dataclass(frozen=True, slots=True)
class EnvelopeUnwrap:
    """Decode node: the wire arguments are ``{key: <semantic arguments>}``."""

    key: str = "pipeline"
''',
    "wire.StripNull": r'''@dataclass(frozen=True, slots=True)
class StripNull:
    """Decode node: a ``null`` at this property path means "omitted" in S."""

    path: tuple[str, ...]
''',
    "wire.EmptyArgumentsMarker": r'''@dataclass(frozen=True, slots=True)
class EmptyArgumentsMarker:
    """A required true wire marker represents the one empty semantic object."""

    key: str = "_elspeth_no_arguments"
''',
    "dispatch._closed_root_schema": r'''def _closed_root_schema(tool_name: str) -> dict[str, Any]:
    """Return the tool's argument schema with a fail-closed root object."""
    schema = cast(dict[str, Any], deep_thaw(_TOOL_SCHEMA_BY_NAME[tool_name]))
    if schema["type"] == "object" and "additionalProperties" not in schema:
        schema = {**schema, "additionalProperties": False}
    return schema
''',
    "dispatch._schema_errors": r"""def _schema_errors(tool_name: str, arguments: object) -> list[ValidationError]:
    return _errors_against(Draft202012Validator(_closed_root_schema(tool_name)), arguments)
""",
    "dispatch._errors_against": r"""def _errors_against(validator: Draft202012Validator, arguments: object) -> list[ValidationError]:
    return sorted(validator.iter_errors(arguments), key=lambda error: tuple(error.absolute_path))
""",
    "dispatch.require_schema_valid_arguments": r'''def require_schema_valid_arguments(tool_name: str, arguments: object) -> None:
    """Hold arguments to the tool's closed-root flat schema S, or raise.

    The dispatch-outside-``execute_tool`` carve-outs call this before their
    pydantic models, so every advertised tool is admitted by the same S gate.
    """
    errors = _schema_errors(tool_name, arguments)
    if errors:
        raise _schema_tool_argument_error(tool_name, errors, _closed_root_schema(tool_name))
''',
}


OWNER_TEMPLATES: dict[str, str] = {
    "batch._register_continuation_assistant_message": r"""def _register_continuation_assistant_message(
    ctx: ToolBatchContext,
    call_model: _CallModelOutcome,
    message: dict[str, Any],
    *,
    tool_call_id: str | None = None,
) -> None:
    if ctx.message_producers is None:
        return
    from elspeth.web.composer.continuation_codec import encode_continuation_response

    ctx.message_producers.require_context(session_id=ctx.session_id, context=ctx.session_operation_context, actor_user_id=ctx.user_id)
    if call_model.audit_call is None:
        raise AuditIntegrityError("current tool message lost its original physical audit")
    response = encode_continuation_response(call_model.completion, audit_call=call_model.audit_call)
    ctx.message_producers.register_tool_call_message(
        response=response,
        completion=call_model.completion,
        message=message,
        tool_call_id=tool_call_id,
    )
""",
    "batch._replace_llm_tool_call_arguments": r'''def _replace_llm_tool_call_arguments(
    llm_messages: Sequence[dict[str, Any]],
    *,
    tool_call_id: str,
    arguments: Mapping[str, Any],
    dialect: ToolContractDialect,
    semantic: bool,
    ctx: ToolBatchContext | None = None,
    call_model: _CallModelOutcome | None = None,
) -> None:
    """Replace the latest assistant call with custody-safe arguments.

    The compose loop appends the provider-authored assistant message before
    dispatch.  A subsequent provider turn must not receive raw inline bytes
    from that history after ELSPETH has intercepted them for proposal custody.
    ``arguments`` are always the flat internal shape; set_pipeline is
    re-enveloped only while serializing the provider transcript.

    ``semantic`` says whether ``arguments`` are the tool's semantic arguments
    (encoded back to the wire form of ``dialect`` through
    ``encode_semantic_arguments``) or a redaction sentinel, which keeps the
    plain ``{"pipeline": ...}`` wrap. In S1 both give the same bytes, because
    set_pipeline is non-strict on every dialect.

    The transcript keeps the model's key order: map order in ``sources``,
    ``row_union.branches`` and ``coalesce.branches`` is semantic, so a sorted
    re-serialisation would show the model an order it never authored.
    """
    for message in reversed(llm_messages):
        if "role" not in message or message["role"] != "assistant":
            continue
        tool_calls = message["tool_calls"] if "tool_calls" in message else None
        if type(tool_calls) is not list:
            continue
        for call in tool_calls:
            if type(call) is not dict:
                continue
            if "id" not in call or call["id"] != tool_call_id:
                continue
            function = call["function"] if "function" in call else None
            if type(function) is not dict:
                raise AuditIntegrityError("Assistant tool call has malformed function envelope")
            if "name" not in function or type(function["name"]) is not str:
                raise AuditIntegrityError("Assistant tool call has malformed function envelope")
            function_name = function["name"]
            provider_arguments: Mapping[str, Any]
            if function_name != "set_pipeline":
                provider_arguments = arguments
            elif semantic:
                provider_arguments = encode_semantic_arguments(function_name, dialect, arguments)
            else:
                provider_arguments = {"pipeline": arguments}
            encoded = json.dumps(provider_arguments, separators=(",", ":"))
            function["arguments"] = encoded
            _retain_llm_tool_argument_parts(llm_messages, tool_call_id=tool_call_id, ctx=ctx, call_model=call_model)
            return
    raise AuditIntegrityError("Assistant tool call was not present in the active LLM transcript")
''',
    "builders.native_assistant_message": r'''def native_assistant_message(completion: _AdmittedLLMCompletion) -> dict[str, Any]:
    """The existing actual tool-batch assistant construction, unchanged."""
    if type(completion) is not _AdmittedLLMCompletion:
        raise TypeError("native assistant construction requires its actual admitted completion")
    return attach_provider_thinking(
        {
            "role": "assistant",
            "content": completion.message.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in completion.tool_batch.calls
            ],
        },
        completion.provider_metadata.thinking_blocks,
    )
''',
    "producers.CurrentContinuationMessageProducers.require_context": r"""def require_context(self, *, session_id: str | None, context: object, actor_user_id: str | None) -> None:
    if (
        session_id != self.inputs.bootstrap.logical.session_id
        or context is not self.inventory.running.session_operation_context
        or actor_user_id != self.inputs.bootstrap.actor_user_id
    ):
        raise AuditIntegrityError("live message producer crossed its actual current context")
""",
    "producers.CurrentContinuationMessageProducers.__post_init__": r"""def __post_init__(self) -> None:
    from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority

    if (
        type(self.service) is not ComposerServiceImpl
        or type(self.authority) is not ComposerAsyncOperationAuthority
        or type(self.selected) is not SelectedContinuationState
        or type(self.inventory) is not CurrentContinuationEnvInventory
        or type(self.inputs) not in (_CurrentProducerInputs, _ReadyProducerInputs)
    ):
        raise AuditIntegrityError("message producer requires its actual current owners")
    if self.finish_read is not None:
        if type(self.finish_read) is not ContinuationFinishRead:
            raise AuditIntegrityError("message producer received a foreign owed-finish facet")
        self.finish_read.require_selection(self.selected, self.inventory)
    sessions = self.service._require_sessions_service()
    if type(sessions) is not SessionServiceImpl or sessions._engine is not self.authority._engine:
        raise AuditIntegrityError("message producer crossed actual session stores")
    states = producer_input_states(self.inputs.producer_inputs, env_ref_names=self.inventory.names)
    for entry in self.inputs.producer_inputs.schema_inputs:
        self._schema_inputs.append(
            (
                _AdmittedToolCall(entry.tool_call_id, _AdmittedToolFunction("get_plugin_schema", entry.raw_arguments)),
                states[entry.state_index],
            )
        )
""",
    "producers.CurrentContinuationMessageProducers.advance_current": r'''def advance_current(self, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory) -> None:
    """Rebind one live producer after its real same-owner SQL transition.

    Fresh source admission precedes installation. Existing SDK message and
    schema objects stay owned here; this read never dispatches or restores
    replacements for those objects, and cannot cross a physical adoption.
    """
    if (
        type(selected) is not SelectedContinuationState
        or type(inventory) is not CurrentContinuationEnvInventory
        or type(self.inputs) is not _CurrentProducerInputs
        or inventory.running is not self.inventory.running
        or inventory.running.session_operation_context is not self.inventory.running.session_operation_context
        or inventory.names != self.inventory.names
    ):
        raise AuditIntegrityError("live message advance changed its actual running/context/inventory owners")
    before, after = self.selected.stack, selected.stack
    if (
        after.logical != before.logical
        or after.cursor <= before.cursor
        or after.admission_policy_json != before.admission_policy_json
        or after.policy_hash != before.policy_hash
        or after.mutation_ceiling != before.mutation_ceiling
        or after.provider_cost_policy != before.provider_cost_policy
    ):
        raise AuditIntegrityError("live message advance changed its request, retained policy or predecessor order")
    selected.__post_init__()
    from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead

    finish_read = None if self.finish_read is None else ContinuationFinishRead(inventory.running, selected, inventory)
    with locked_session_transaction(self.authority._engine, str(inventory.running.claim.session_id)) as conn:
        from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn

        try:
            actual = _read_current_inputs(conn, selected=selected, inventory=inventory, finish_read=finish_read)
        except ComposerOperationCancelledDuringTurn:
            finish_read = ContinuationFinishRead(inventory.running, selected, inventory)
            actual = _read_current_inputs(conn, selected=selected, inventory=inventory, finish_read=finish_read)
        if (
            actual.bootstrap != self.inputs.bootstrap
            or actual.bootstrap.actor_user_id != self.inputs.bootstrap.actor_user_id
            or actual.producer_inputs != capture_continuation_producer_inputs(tuple(self._schema_inputs), env_ref_names=inventory.names)
        ):
            raise AuditIntegrityError("live message advance changed its original actor or actual schema history")
    # No callbacks, allocation of SDK objects or await between these writes.
    # A failed SQL read/transaction leaves every old binding intact.
    self.selected, self.inventory, self.inputs, self.finish_read = selected, inventory, actual, finish_read
''',
    "producers.ContinuationToolMessagePublication": r'''class ContinuationToolMessagePublication:
    """Closed bindings to actual live issued owners for capture, never a write grant."""

    __slots__ = (
        "_sealed",
        "batch",
        "call",
        "catalog",
        "completion",
        "messages",
        "producers",
        "recorder",
    )

    producers: CurrentContinuationMessageProducers
    completion: _AdmittedLLMCompletion
    batch: ContinuationBatchState
    call: ContinuationCallState
    recorder: BufferingRecorder
    messages: list[dict[str, Any]]
    catalog: PolicyCatalogView
    _sealed: bool

    def __new__(
        cls,
        producers: CurrentContinuationMessageProducers,
        completion: _AdmittedLLMCompletion,
        batch: ContinuationBatchState,
        call: ContinuationCallState,
        recorder: BufferingRecorder,
        messages: list[dict[str, object]],
        catalog: PolicyCatalogView,
    ) -> Self:
        owner = object.__new__(cls)
        object.__setattr__(owner, "_sealed", False)
        return owner

    def __init__(
        self,
        producers: CurrentContinuationMessageProducers,
        completion: _AdmittedLLMCompletion,
        batch: ContinuationBatchState,
        call: ContinuationCallState,
        recorder: BufferingRecorder,
        messages: list[dict[str, object]],
        catalog: PolicyCatalogView,
    ) -> None:
        if self._sealed:
            raise FrozenInstanceError("ContinuationToolMessagePublication cannot be reinitialized")
        object.__setattr__(self, "producers", producers)
        object.__setattr__(self, "completion", completion)
        object.__setattr__(self, "batch", batch)
        object.__setattr__(self, "call", call)
        object.__setattr__(self, "recorder", recorder)
        object.__setattr__(self, "messages", messages)
        object.__setattr__(self, "catalog", catalog)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        raise FrozenInstanceError("ContinuationToolMessagePublication bindings cannot be reassigned")

    def __delattr__(self, name: str) -> None:
        raise FrozenInstanceError("ContinuationToolMessagePublication bindings cannot be deleted")

    def __eq__(self, other: object) -> bool:
        if type(self) is not ContinuationToolMessagePublication or type(other) is not ContinuationToolMessagePublication:
            return NotImplemented
        return (
            self.producers,
            self.completion,
            self.batch,
            self.call,
            self.recorder,
            self.messages,
            self.catalog,
        ) == (
            other.producers,
            other.completion,
            other.batch,
            other.call,
            other.recorder,
            other.messages,
            other.catalog,
        )

    def require_call(
        self, *, recorder: BufferingRecorder, audit: DispatchAudit | None, tool_name: str, tool_call_id: str, messages: list[dict[str, Any]]
    ) -> None:
        from elspeth.web.composer.audit import BufferingRecorder, DispatchAudit
        from elspeth.web.composer.tool_batch import ContinuationBatchState, ContinuationCallState

        if (
            type(self.producers) is not CurrentContinuationMessageProducers
            or type(self.completion) is not _AdmittedLLMCompletion
            or type(self.batch) is not ContinuationBatchState
            or type(self.call) is not ContinuationCallState
            or type(self.recorder) is not BufferingRecorder
            or self.recorder is not recorder
            or self.messages is not messages
            or self.batch.current_call is not self.call
            or self.call.audit is not audit
            or type(audit) is not DispatchAudit
            or (self.call.tool_name, self.call.tool_call.id) != (tool_name, tool_call_id)
            or not 0 <= self.call.index < len(self.completion.tool_batch.calls)
            or self.completion.tool_batch.calls[self.call.index] is not self.call.tool_call
        ):
            raise AuditIntegrityError("tool publication changed its actual issued call/recorder/message owners")

    def register_feedback(self, *, serialized: str, message: dict[str, Any]) -> dict[str, Any]:
        return self.producers.register_tool_feedback_message(publication=self, serialized=serialized, message=message)

    def bind_completed_outcome(self, outcome: _ToolOutcome) -> None:
        self.producers._bind_feedback_outcome(self, outcome)

    def register_result(self, *, result: ToolResult, serialized: str, message: dict[str, Any]) -> dict[str, Any]:
        self.require_call(
            recorder=self.recorder,
            audit=self.call.audit,
            tool_name=self.call.tool_name,
            tool_call_id=self.call.tool_call.id,
            messages=self.messages,
        )
        return self.producers.register_tool_result_message(
            completion=self.completion,
            tool_index=self.call.index,
            call=self.call.tool_call,
            result=result,
            serialized=serialized,
            message=message,
            catalog=self.catalog,
        )''',
    "producers.CurrentMessageProduction": r'''class CurrentMessageProduction:
    """Stable membership of actual live messages registered by their owner.

    The original mutable mappings and producer stay strongly referenced: their
    identity and complete wire remain subject to the existing registry checks.
    """

    __slots__ = (
        "_sealed",
        "messages",
        "producers",
    )

    producers: CurrentContinuationMessageProducers
    messages: tuple[Mapping[str, Any], ...] | None
    _sealed: bool

    def __new__(
        cls,
        producers: CurrentContinuationMessageProducers,
        messages: tuple[Mapping[str, Any], ...] | None,
    ) -> Self:
        owner = object.__new__(cls)
        object.__setattr__(owner, "_sealed", False)
        return owner

    def __init__(
        self,
        producers: CurrentContinuationMessageProducers,
        messages: tuple[Mapping[str, Any], ...] | None,
    ) -> None:
        if self._sealed:
            raise FrozenInstanceError("CurrentMessageProduction cannot be reinitialized")
        object.__setattr__(self, "producers", producers)
        object.__setattr__(self, "messages", messages)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        raise FrozenInstanceError("CurrentMessageProduction bindings cannot be reassigned")

    def __delattr__(self, name: str) -> None:
        raise FrozenInstanceError("CurrentMessageProduction bindings cannot be deleted")

    def __eq__(self, other: object) -> bool:
        if type(self) is not CurrentMessageProduction or type(other) is not CurrentMessageProduction:
            return NotImplemented
        return (
            self.producers,
            self.messages,
        ) == (
            other.producers,
            other.messages,
        )''',
    "producers._produce_current_messages": r"""def _produce_current_messages(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    retained_shelf: MessageShelfSnapshot | None,
    initial: bool,
    finish_read: ContinuationFinishRead | None = None,
) -> CurrentMessageProduction:
    from elspeth.web.composer.continuation_message_shelf import MessageShelfSnapshot, encode_message_shelf
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority

    if type(service) is not ComposerServiceImpl or type(authority) is not ComposerAsyncOperationAuthority:
        raise AuditIntegrityError("message production requires actual service and SQL authority owners")
    if type(selected) is not SelectedContinuationState or type(inventory) is not CurrentContinuationEnvInventory:
        raise AuditIntegrityError("message production requires actual current selection and inventory")
    if finish_read is not None and initial:
        raise AuditIntegrityError("owed-finish restoration cannot create a new initial dialogue")
    with locked_session_transaction(authority._engine, str(inventory.running.claim.session_id)) as conn:
        inputs = _read_current_inputs(conn, selected=selected, inventory=inventory, finish_read=finish_read)
    owner = CurrentContinuationMessageProducers(service, authority, selected, inventory, inputs, finish_read=finish_read)
    if retained_shelf is not None:
        if (
            type(retained_shelf) is not MessageShelfSnapshot
            or encode_message_shelf(retained_shelf).canonical != inputs.evidence_messages_json
            or initial
        ):
            raise AuditIntegrityError("message production changed the selected evidence shelf")
        messages = owner.restore_shelf(retained_shelf)
        return CurrentMessageProduction(owner, tuple(messages[index] for index in retained_shelf.current_indexes))
    return CurrentMessageProduction(owner, tuple(owner.initial_messages()) if initial else None)
""",
    "producers.load_current_message_producers": r"""async def load_current_message_producers(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    required_work: RequiredWorkBinding,
    retained_shelf: MessageShelfSnapshot | None = None,
    initial: bool = False,
    finish_read: ContinuationFinishRead | None = None,
) -> CurrentMessageProduction:
    if type(required_work) is not RequiredWorkBinding or type(inventory) is not CurrentContinuationEnvInventory:
        raise AuditIntegrityError("message production lost its required-work/current inventory")
    running = required_work.running
    if type(running) is not ComposerOperationRunning or inventory.running is not running:
        raise AuditIntegrityError("message production changed its current running owner")
    required_work.validate_context(running.session_operation_context)
    custody = required_work.coordinator.authority
    if (
        custody.authority_kind is not RequiredAuthorityKind.DURABLE_COMPOSE
        or custody.durable_operation_id != running.claim.operation_id
        or custody.claim_attempt != running.claim.attempt
    ):
        raise AuditIntegrityError("message production changed its required-work claim")
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(
            sql,
            _produce_current_messages,
            service=service,
            authority=authority,
            selected=selected,
            inventory=inventory,
            retained_shelf=retained_shelf,
            initial=initial,
            finish_read=finish_read,
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    try:
        required_work.coordinator.validate_continuation_sql_outcome(
            ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=outcome
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    errors: list[BaseException] = []
    actual: CurrentMessageProduction | None = None
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        errors.append(outcome.error)
    elif type(outcome) is RequiredSQLReturned:
        projection.begin_projection()
        try:
            if type(outcome.value) is not CurrentMessageProduction:
                raise AuditIntegrityError("message projection lost its actual produced owner")
            actual = outcome.value
        except BaseException as error:
            projection.complete_owned(error)
            errors.append(error)
        else:
            projection.complete_owned()
    else:
        projection.complete_without_submission()
        errors.append(AuditIntegrityError("message production returned an unknown SQL outcome"))
    for cancellation in outcome.deferred_cancellations:
        if all(cancellation is not original for original in errors):
            errors.append(cancellation)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Continuation current message outcomes", errors)
    if actual is None:
        raise AuditIntegrityError("message production lost its result")
    return actual
""",
    "failure.ContinuationToolFailureCustody.__post_init__": r"""def __post_init__(self) -> None:
    if (
        self.work.running is not self.running
        or self.call.tool_call is not self.calls[self.call.index]
        or self.batch.current_call is not self.call
        or self.call.audit is None
    ):
        raise AuditIntegrityError("tool failure custody changed its actual issued owners")
    self.observation = ContinuationDispatchFailureObservation(self, self.call.audit)
""",
    "failure.ContinuationDispatchFailureObservation.require": r"""def require(self, recorder: BufferingRecorder, audit: DispatchAudit) -> None:
    if recorder is not self.owner.recorder or audit is not self.audit:
        raise AuditIntegrityError("dispatch failure observation belongs to another actual audit")
""",
    "runtime.RestoredContinuationRuntime": r"""@dataclass(frozen=True, slots=True)
class RestoredContinuationRuntime:
    live: _ComposeIterationState
    bindings: _ComposeIterationBindings
    graph: RestoredTurnEvidence
    materialization: CurrentContinuationMessageProducers
    source: ContinuationRuntimeSource
    drained: RecorderDrainCursors
    attachments: ContinuationAttachmentHistory
    env_ref_names: frozenset[str]
""",
    "runtime._CurrentPhaseRead": r'''@dataclass(frozen=True, slots=True)
class _CurrentPhaseRead:
    """One phase's actual read cohort; every use still re-enters SQL proof."""

    runtime: RestoredContinuationRuntime
    selected: SelectedContinuationState
    inventory: CurrentContinuationEnvInventory
    finish_read: ContinuationFinishRead | None
''',
    "runtime._registered_read": r"""async def _registered_read[T](work: RequiredWorkBinding, owner: type[T], operation: Callable[[], T]) -> T:
    sql, projection = work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(sql, operation)
    except BaseException:
        projection.complete_without_submission()
        raise
    try:
        work.coordinator.validate_continuation_sql_outcome(
            ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=outcome
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    failures: list[BaseException] = []
    result: T | None = None
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        failures.append(outcome.error)
    elif type(outcome) is RequiredSQLReturned:
        projection.begin_projection()
        try:
            if type(outcome.value) is not owner:
                raise AuditIntegrityError("runtime projection changed its actual nominal returned owner")
            result = outcome.value
        except BaseException as error:
            projection.complete_owned(error)
            failures.append(error)
        else:
            projection.complete_owned()
    else:
        projection.complete_without_submission()
        raise AuditIntegrityError("runtime returned an unfamiliar required-SQL outcome")
    for cancellation in outcome.deferred_cancellations:
        if all(cancellation is not original for original in failures):
            failures.append(cancellation)
    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise BaseExceptionGroup("Continuation runtime original outcomes", failures)
    if result is None:
        raise AuditIntegrityError("runtime lost its complete returned owner")
    return result
""",
    "runtime._restore_live": r"""def _restore_live(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner,
    source: ContinuationRuntimeSource,
    graph: RestoredTurnEvidence,
    materialization: CurrentContinuationMessageProducers,
    monotonic_before_read: float,
    progress: ComposerProgressSink | None,
    finish_read: ContinuationFinishRead | None = None,
) -> RestoredContinuationRuntime:
    from elspeth.web.composer.service import _chat_ingress_inputs_for_compose

    _index, frame = _compose(selected)
    names = inventory.names
    original = source.original
    initial = graph.evidence.effects.initial_state
    if frame.state.codec != "composer.state.v1":
        raise AuditIntegrityError("runtime current state changed its actual closed codec")
    selected_state = runtime_chunk(selected, frame.state).canonical
    if graph.batch is not None:
        state = graph.batch.batch.state
    else:
        from elspeth.web.composer.continuation_evidence import state_from_evidence_json

        state = state_from_evidence_json(selected_state)
    if (
        canonical_json(state.to_dict()) != selected_state
        or runtime_chunk(selected, frame.initial_state).canonical != original.snapshot.initial_state_json
        or canonical_json(initial.to_dict()) != original.snapshot.initial_state_json
        or initial.version != frame.initial_version
        or frame.current_state_id != graph.evidence.effects.latest_committed_state_id
    ):
        raise AuditIntegrityError("runtime changed original base or actual shared state/frontier")
    require_no_credential_material_in_state(state, surface="current continuation runtime state", env_ref_names=names)
    if runtime_chunk(selected, frame.audit_buffer) != encode_recorder_snapshot(
        graph.recorder.snapshot_for_continuation(drained=graph.drained)
    ):
        raise AuditIntegrityError("runtime recorder differs from its one shared evidence graph")
    if runtime_chunk(selected, frame.transcript) != encode_message_shelf(source.evidence.transcript):
        raise AuditIntegrityError("runtime transcript differs from its complete selected shelf")
    expected_messages = canonical_json(
        {"messages_json": source.evidence.transcript.messages_json, "current_indexes": source.evidence.transcript.current_indexes}
    )
    if runtime_chunk(selected, frame.messages).canonical != expected_messages:
        raise AuditIntegrityError("runtime messages differ from the shared selected transcript")
    snapshot, catalog = service._policy_context.build(original.snapshot.actor_user_id)
    current = catalog.validate_composition_state(state)
    if canonical_json(current.authored_state.to_dict()) != selected_state:
        raise AuditIntegrityError("current policy requires authored-state reinspection before runtime restore")
    composition_scope_for_operation(
        state,
        session_id=selected.stack.logical.session_id,
        session_operation_context=inventory.running.session_operation_context,
        session_operation_authority=service._require_sessions_service().session_operation_authority,
    )
    tools = composer_loop_tool_definitions(service._planner_dialect)
    if (
        frame.tools is None
        or frame.tools.codec != "composer.tools.v1"
        or runtime_chunk(selected, frame.tools).canonical != canonical_json(tools)
    ):
        raise AuditIntegrityError("runtime tool contract changed and requires current reacquisition")
    if graph.selected_dialect is not None and graph.selected_dialect is not service._planner_dialect:
        raise AuditIntegrityError("runtime prepared wire dialect changed and requires current reacquisition")
    values = iteration_values(source.iteration, env_ref_names=names)
    cache_history = decode_continuation_caches(runtime_chunk(selected, frame.caches), env_ref_names=names)
    if cache_history.session_id != selected.stack.logical.session_id:
        raise AuditIntegrityError("runtime cache history belongs to another session")
    caches = restore_continuation_caches(cache_history, env_ref_names=names)
    attachments = source.attachments
    if (frame.persisted_assistant_message_id is None) != (frame.persisted_assistant_content is None):
        raise AuditIntegrityError("runtime lost its paired persisted assistant identity/content")
    if any(type(index) is not int or not 0 <= index < len(graph.messages) for index in frame.pending_advisor_elision_indices):
        raise AuditIntegrityError("runtime pending Advisor elision lost its exact message positions")
    ceiling = selected.stack.mutation_ceiling.materialize()
    ceiling.validate_context(inventory.running.session_operation_context)
    live = _ComposeIterationState(
        state=state,
        initial_version=frame.initial_version,
        llm_messages=graph.messages,
        tools=tools,
        recorder=graph.recorder,
        composition_turns_used=frame.composition_slice,
        discovery_turns_used=frame.discovery_slice,
        mutation_success_seen=frame.mutation_success_seen,
        discovery_cache=caches.discovery,
        last_validation=current.validation,
        runtime_preflight_cache=caches.preflight,
        last_runtime_preflight=caches.last_preflight,
        anti_anchor=AntiAnchorTracker.for_continuation(source.iteration.anti_anchor),
        advisor_calls_used=frame.advisor_calls,
        repair_turns_used=frame.repair_turns,
        advisor_checkpoint_passes_used=frame.advisor_checkpoint_passes,
        advisor_review_state=values.advisor_review,
        persisted_assistant_message_id=frame.persisted_assistant_message_id,
        persisted_assistant_content=frame.persisted_assistant_content,
        persisted_tool_call_turn=frame.persisted_tool_call_turn,
        failed_turn=values.failed_turn,
        current_state_id=frame.current_state_id,
        advisor_repair_context_introduced=frame.advisor_repair_context_introduced,
        pending_advisor_elision_indices=list(frame.pending_advisor_elision_indices),
        turn_evidence=graph.evidence,
        continuation_cache_history=caches.history,
    )
    bindings = _ComposeIterationBindings(
        message=original.message,
        session_id=selected.stack.logical.session_id,
        user_id=original.snapshot.actor_user_id,
        user_message_id=selected.stack.logical.original_user_message_id,
        deadline=monotonic_before_read + source.timing.hard_remaining_seconds,
        progress=progress,
        plugin_snapshot=snapshot,
        policy_catalog=catalog,
        mutation_ceiling=ceiling,
        session_operation_context=inventory.running.session_operation_context,
        completion_gates=original.completion_gates,
        diagnostics=None,
        required_work=work,
        provider_owner=provider_owner,
        ingress=compartment_ingress_record(original.message, own_compartment_id=service._settings.compartment_id),
        chat_ingress_inputs=_chat_ingress_inputs_for_compose(
            original.message,
            list(original.history),
            user_message_id=selected.stack.logical.original_user_message_id,
            own_compartment_id=service._settings.compartment_id,
        ),
        actor=f"composer-web:user-{original.snapshot.actor_user_id}",
        session_scope=f"session:{selected.stack.logical.session_id}",
    )
    # A later read can only shorten admission; never use its newer remaining
    # observation to renew the monotonic anchor or original logical budgets.
    if finish_read is None:
        authority.read_continuation_runtime(inventory.running, selected=selected, inventory=inventory)
    else:
        finish_read.require_selection(selected, inventory)
        finish_read.read_runtime(authority)
    service._schema_disclosure.restore_session_for_continuation(
        source.iteration.disclosure,
        session_id=selected.stack.logical.session_id,
        catalog_hash=snapshot.snapshot_hash,
        policy_hash=snapshot.policy_hash,
    )
    return RestoredContinuationRuntime(live, bindings, graph, materialization, source, graph.drained, attachments, names)
""",
    "runtime.restore_continuation_runtime": r"""async def restore_continuation_runtime(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner,
    progress: ComposerProgressSink | None = None,
    finish_read: ContinuationFinishRead | None = None,
) -> RestoredContinuationRuntime:
    from elspeth.web.composer.service import ComposerServiceImpl

    running = _work_owners(authority, inventory, required_work)
    if (
        type(service) is not ComposerServiceImpl
        or type(provider_owner) is not ProviderInvocationOwner
        or provider_owner.required_work is not required_work
        or provider_owner.service is not service._require_sessions_service()
    ):
        raise AuditIntegrityError("runtime lost actual current service/provider-work custody")
    monotonic_before_read = time.monotonic()
    if finish_read is not None:
        from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead

        if type(finish_read) is not ContinuationFinishRead:
            raise AuditIntegrityError("runtime restore received a foreign owed-finish facet")
        finish_read.require_selection(selected, inventory)
    source = await _registered_read(
        required_work,
        ContinuationRuntimeSource,
        lambda: (
            authority.read_continuation_runtime(running, selected=selected, inventory=inventory)
            if finish_read is None
            else finish_read.read_runtime(authority)
        ),
    )
    if type(source) is not ContinuationRuntimeSource:
        raise AuditIntegrityError("runtime source returned another nominal owner")
    production = await load_current_message_producers(
        service=service, authority=authority, selected=selected, inventory=inventory, required_work=required_work, finish_read=finish_read
    )
    graph = await restore_current_turn_evidence(
        service=service,
        authority=authority,
        selected=selected,
        inventory=inventory,
        required_work=required_work,
        materialization=production.producers,
    )
    result = await _registered_read(
        required_work,
        RestoredContinuationRuntime,
        lambda: _restore_live(
            service=service,
            authority=authority,
            selected=selected,
            inventory=inventory,
            work=required_work,
            provider_owner=provider_owner,
            source=source,
            graph=graph,
            materialization=production.producers,
            monotonic_before_read=monotonic_before_read,
            progress=progress,
            finish_read=finish_read,
        ),
    )
    if type(result) is not RestoredContinuationRuntime:
        raise AuditIntegrityError("runtime restore returned another nominal owner")
    return result
""",
    "runtime.build_runtime_tool_batch_context": r"""async def build_runtime_tool_batch_context(
    runtime: RestoredContinuationRuntime,
    *,
    cancellation_requested: asyncio.Event,
    phase_read: _CurrentPhaseRead | None = None,
) -> ToolBatchContext:
    if type(runtime) is not RestoredContinuationRuntime or type(cancellation_requested) is not asyncio.Event:
        raise AuditIntegrityError("runtime dispatch context requires its actual owners")
    live, bindings = runtime.live, runtime.bindings
    service = runtime.materialization.service
    work = bindings.required_work
    if type(work) is not RequiredWorkBinding:
        raise AuditIntegrityError("runtime context lost its required-work binding")
    authority = runtime.materialization.authority
    selected, inventory = runtime.source.selected, runtime.materialization.inventory
    finish_read: ContinuationFinishRead | None = None
    if phase_read is not None:
        from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead

        if (
            type(phase_read) is not _CurrentPhaseRead
            or phase_read.runtime is not runtime
            or phase_read.selected is not selected
            or type(phase_read.inventory) is not CurrentContinuationEnvInventory
        ):
            raise AuditIntegrityError("runtime context replaced its actual phase read cohort")
        inventory, finish_read = phase_read.inventory, phase_read.finish_read
        if finish_read is not None:
            if type(finish_read) is not ContinuationFinishRead:
                raise AuditIntegrityError("runtime context received a foreign owed-read facet")
            finish_read.require_selection(selected, inventory)
    running = _work_owners(authority, inventory, work)
    if (
        bindings.session_operation_context is not running.session_operation_context
        or bindings.session_id != str(running.claim.session_id)
        or selected.stack.logical.session_id != str(running.claim.session_id)
        or selected.stack.logical.operation_id != running.claim.operation_id
        or (finish_read is not None and finish_read.running is not running)
    ):
        raise AuditIntegrityError("runtime context crossed its actual phase session/current owner")

    def read_source() -> ContinuationRuntimeSource:
        if finish_read is not None:
            finish_read.require_selection(selected, inventory)
            source = finish_read.read_runtime(authority)
        else:
            source = authority.read_continuation_runtime(running, selected=selected, inventory=inventory)
        if type(source) is not ContinuationRuntimeSource or source.selected is not selected:
            raise AuditIntegrityError("runtime context lost its actual selected SQL read")
        return source

    sessions = service._require_sessions_service()
    await _registered_read(work, ContinuationRuntimeSource, read_source)
    sid = UUID(runtime.source.selected.stack.logical.session_id)
    preferences = await sessions.get_composer_preferences(sid)
    await _registered_read(work, ContinuationRuntimeSource, read_source)
    ceiling = bindings.mutation_ceiling
    if ceiling is None or bindings.session_operation_context is not running.session_operation_context:
        raise AuditIntegrityError("runtime context lost its original mutation ceiling/current fence")
    ceiling.validate_context(running.session_operation_context)
    if ceiling.request_id != live.turn_evidence.request_id:
        raise AuditIntegrityError("runtime dispatch crossed its original logical request")
    return ToolBatchContext(
        session_tools=service._session_tools,
        provenance=ToolBatchProvenance(
            model_identifier=service._model, provider=service._availability.provider, skill_hash=service._composer_skill_hash
        ),
        custody=ToolBatchCustody(
            data_dir=service._data_dir,
            session_engine=service._session_engine,
            secret_service=service._secret_service,
            secret_wiring_policy=service._secret_wiring_policy,
            max_blob_storage_per_session_bytes=service._settings.max_blob_storage_per_session_bytes,
        ),
        redaction_telemetry=service._redaction_telemetry,
        preflight=service._preflight,
        schema_disclosure=service._schema_disclosure,
        advisor_checkpoint=service._advisor_checkpoint,
        advisor_max_calls_per_compose=service._settings.composer_advisor_max_calls_per_compose,
        advisor_timeout_seconds=service._settings.composer_advisor_timeout_seconds,
        recorder=live.recorder,
        anti_anchor=live.anti_anchor,
        discovery_cache=live.discovery_cache,
        runtime_preflight_cache=live.runtime_preflight_cache,
        session_id=bindings.session_id,
        session_operation_context=bindings.session_operation_context,
        user_id=bindings.user_id,
        user_message_id=bindings.user_message_id,
        user_message_content=bindings.message,
        current_state_id=live.current_state_id,
        actor=bindings.actor,
        initial_version=live.initial_version,
        deadline=bindings.deadline,
        progress=bindings.progress,
        session_scope=bindings.session_scope,
        turn_sessions_service=sessions,
        turn_session_uuid=sid,
        turn_preferences=preferences,
        mutation_ceiling=ceiling,
        review_required=not ceiling.permits_auto(preferences.trust_mode),
        cancellation_requested=cancellation_requested,
        plugin_snapshot=bindings.plugin_snapshot,
        policy_catalog=bindings.policy_catalog,
        tool_contract_dialect=service._planner_dialect,
        turn_evidence=live.turn_evidence,
        session_operation_authority=sessions.session_operation_authority,
        provider_owner=bindings.provider_owner,
        composition_turns_used=live.composition_turns_used,
        discovery_turns_used=live.discovery_turns_used,
        failed_turn=live.failed_turn,
        message_producers=runtime.materialization,
    )
""",
    "first.FirstRuntimeGraph": r"""@dataclass(frozen=True, slots=True)
class FirstRuntimeGraph:
    source: FirstRuntimeSources
    graph: RestoredTurnEvidence
    materialization: CurrentContinuationMessageProducers
""",
    "first._restore_first_graph": r"""def _restore_first_graph(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    work: RequiredWorkBinding,
    expected: FirstRuntimeSources,
) -> FirstRuntimeGraph:
    _require_current_reader_service(service, authority)
    running = _work_owners(authority, inventory, work)
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        before = read_first_runtime_sources_on_connection(conn, service, running, selected=selected, inventory=inventory)
        _require_same_sources(expected, before)
    # Nothing from the ended SQL window is a live admission capability.
    # These are provisional values; all DATA are independently reread below.
    inputs, snapshot = before.inputs, before.runtime.evidence
    producer = CurrentContinuationMessageProducers(service, authority, selected, inventory, inputs)
    producer.require_context(
        session_id=selected.stack.logical.session_id,
        context=running.session_operation_context,
        actor_user_id=inputs.bootstrap.actor_user_id,
    )
    evidence, initial, latest = _begin_current_evidence(snapshot, context=running.session_operation_context)
    history = AdvisorTranscriptOwner._continuation_history(
        snapshot.transcript,
        session_id=snapshot.session_id,
        actor_user_id=snapshot.actor_user_id,
        request_id=snapshot.request_id,
        request=snapshot.transcript.request,
    )
    # Actual initial/policy/schema/content-scope callbacks remain outside SQL.
    shelf = producer._rebuild_shelf(snapshot.transcript, inputs=inputs)
    evidence.transcript, messages = AdvisorTranscriptOwner._from_continuation_shelf(
        snapshot.transcript, request=snapshot.transcript.request, history=history, shelf=shelf
    )
    reader = CurrentTranscriptBlobReader.from_service(
        service, session_id=str(running.claim.session_id), context=running.session_operation_context
    )
    graph = _finish_current_evidence(
        inputs.admitted_evidence,
        context=running.session_operation_context,
        owner=evidence,
        initial=initial,
        latest=latest,
        messages=messages,
        reader=reader,
        env_ref_names=inventory.names,
    )
    # This fresh complete SQL window is AFTER shelf/graph callbacks and BEFORE
    # any live callback. No earlier currentness/decode survives as authority.
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        after = read_first_runtime_sources_on_connection(conn, service, running, selected=selected, inventory=inventory)
        _require_same_sources(before, after)
    return FirstRuntimeGraph(expected, graph, producer)
""",
    "first._registered_graph": r"""async def _registered_graph(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    work: RequiredWorkBinding,
    expected: FirstRuntimeSources,
) -> FirstRuntimeGraph:
    sql, projection = work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(
            sql,
            _restore_first_graph,
            service=service,
            authority=authority,
            selected=selected,
            inventory=inventory,
            work=work,
            expected=expected,
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    # The original evidence projection owns nominality, error grouping and
    # identity-deduplicated deferred cancellation before any live submission.
    return _finish_evidence_projection(work, sql, projection, outcome, expected=FirstRuntimeGraph)
""",
    "first.restore_first_continuation_runtime": r"""async def restore_first_continuation_runtime(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner,
    progress: ComposerProgressSink | None = None,
) -> RestoredContinuationRuntime:
    from elspeth.web.composer.service import ComposerServiceImpl

    _work_owners(authority, inventory, required_work)
    if (
        type(service) is not ComposerServiceImpl
        or type(provider_owner) is not ProviderInvocationOwner
        or provider_owner.required_work is not required_work
        or provider_owner.service is not service._require_sessions_service()
    ):
        raise AuditIntegrityError("runtime lost actual current service/provider-work custody")
    monotonic_before_read = time.monotonic()
    source = await _registered_read(
        required_work,
        FirstRuntimeSources,
        lambda: _read_first_sources(service=service, authority=authority, selected=selected, inventory=inventory, work=required_work),
    )
    # Real pre-callback finish-once/projection handoff: cancellation retained
    # during source/loading work is raised BEFORE the graph worker can start.
    # The graph worker performs a NEW current read after this await; source is
    # comparison DATA only, never cross-window currentness or a renewed clock.
    restored = await _registered_graph(
        service=service, authority=authority, selected=selected, inventory=inventory, work=required_work, expected=source
    )
    # REQUIRED cancellation/error/projection handoff: the actual existing
    # finish-once owner raises originals BEFORE this second worker is submitted.
    # Never combine these operations in a single noncancellable worker.
    return await _registered_read(
        required_work,
        RestoredContinuationRuntime,
        lambda: _restore_live(
            service=service,
            authority=authority,
            selected=selected,
            inventory=inventory,
            work=required_work,
            provider_owner=provider_owner,
            source=restored.source.runtime,
            graph=restored.graph,
            materialization=restored.materialization,
            monotonic_before_read=monotonic_before_read,
            progress=progress,
        ),
    )
""",
    "phases.ContinuationLivePhases._tool_context": r"""async def _tool_context(self, runtime: RestoredContinuationRuntime) -> ToolBatchContext:
    context = await build_runtime_tool_batch_context(runtime, cancellation_requested=self.cancellation_requested)
    owner = self.work.coordinator.sql_deadline_owner
    if owner is not None:
        if owner.running is not runtime.materialization.inventory.running:
            raise AuditIntegrityError("tool context changed its actual original timing owner")
        now = time.monotonic()
        remaining = owner.remaining_dispatch(monotonic_now=now)
        if remaining <= 0:
            raise TimeoutError("tool action exhausted its original dispatch allowance")
        context = replace(context, deadline=min(context.deadline, now + remaining))
    return context
""",
    "phases.ContinuationLivePhases.bind_initial": r"""def bind_initial(self, runtime: RestoredContinuationRuntime) -> None:
    if (
        self.runtime is not None
        or type(runtime) is not RestoredContinuationRuntime
        or runtime.materialization.service is not self.service
        or runtime.materialization.authority is not self.authority
        or runtime.bindings.required_work is not self.work
        or runtime.bindings.provider_owner is not self.provider_owner
    ):
        raise AuditIntegrityError("live phase initialization changed its actual runtime owners")
    self.runtime = runtime
""",
    "phases.ContinuationLivePhases.current": r"""async def current(self, selected: SelectedContinuationState) -> tuple[RestoredContinuationRuntime, CurrentContinuationEnvInventory]:
    phase = await self._current_phase_read(selected)
    return phase.runtime, phase.inventory
""",
    "phases.ContinuationLivePhases._current_phase_read": r"""async def _current_phase_read(self, selected: SelectedContinuationState) -> _CurrentPhaseRead:
    running = self.work.running
    if type(running) is not ComposerOperationRunning or running.session_operation_context is not self.lease.context:
        raise AuditIntegrityError("live phase lost its actual running/physical owner")
    finish_read: ContinuationFinishRead | None = None
    try:
        inventory = await materialize_continuation_env_inventory(
            advisor=self.service._advisor_checkpoint, authority=self.authority, selected=selected, required_work=self.work
        )
    except ComposerOperationCancelledDuringTurn:
        names = await _read_current_env_names(self.service._advisor_checkpoint, selected.stack.logical.session_id)
        inventory = CurrentContinuationEnvInventory(
            running,
            ContinuationEnvSelection(selected.checkpoint_id, selected.stack.cursor, encode_continuation_snapshot(selected.stack)),
            names,
        )
        finish_read = ContinuationFinishRead(running, selected, inventory)

    def read_source() -> ContinuationRuntimeSource:
        nonlocal finish_read
        if finish_read is not None:
            return finish_read.read_runtime(self.authority)
        try:
            return self.authority.read_continuation_runtime(inventory.running, selected=selected, inventory=inventory)
        except ComposerOperationCancelledDuringTurn:
            finish_read = ContinuationFinishRead(running, selected, inventory)
            return finish_read.read_runtime(self.authority)

    source = await _registered_read(self.work, ContinuationRuntimeSource, read_source)
    if self.runtime is None:
        self.runtime = await restore_continuation_runtime(
            service=self.service,
            authority=self.authority,
            selected=selected,
            inventory=inventory,
            required_work=self.work,
            provider_owner=self.provider_owner,
            finish_read=finish_read,
        )
    elif self.runtime.source.selected != selected:
        runtime = self.runtime

        def refresh() -> RestoredContinuationRuntime:
            fresh = read_source()
            _require_runtime_source_refresh(source, fresh)
            runtime.materialization.advance_current(selected, inventory)
            following = replace(runtime, source=fresh)
            # Retain the actual fresh source before the async SQL bridge can
            # defer cancellation. No live messages/graph objects are rebuilt.
            self.runtime = following
            return following

        await _registered_read(self.work, RestoredContinuationRuntime, refresh)
    else:
        # Same SQL cut can arrive as a fresh selected object, including a
        # warm owner which has just observed durable Stop. Carry that
        # exact read choice without replacing live owners or their clocks.
        _require_runtime_source_refresh(self.runtime.source, source)
        self.runtime = replace(self.runtime, source=source)
    if self.runtime is None:
        raise AuditIntegrityError("live phase source lost its actual runtime")
    if self.runtime.source.selected is not selected:
        raise AuditIntegrityError("live phase read lost its actual fresh selection")
    if finish_read is not None:
        finish_read.require_selection(selected, inventory)
    return _CurrentPhaseRead(self.runtime, selected, inventory, finish_read)
""",
    "phases.ContinuationLivePhases._retain": r"""async def _retain(
    self, captured: CapturedContinuationRuntime, inventory: CurrentContinuationEnvInventory, transition: BatchTransition
) -> None:
    def retain() -> StoredContinuationSnapshot:
        stored = retain_batch_transition(
            self.authority, inventory.running, captured=captured, inventory=inventory, transition=transition
        )
        # Preserve the actual committing owner's return on its physical
        # thread before bridge delivery can surface a caller cancellation.
        self.latest_snapshot = stored
        return stored

    stored = await _registered(
        work=self.work,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL,
        operation=retain,
        expected=StoredContinuationSnapshot,
    )
    selected = await load_selected_continuation(
        authority=self.authority, running=inventory.running, selected=stored, required_work=self.work
    )
    await self.current(selected)
""",
    "phases.ContinuationLivePhases._execute_one_owned": r"""async def _execute_one_owned(self, selected: SelectedContinuationState) -> None:
    runtime, inventory = await self.current(selected)
    data = runtime.graph.batch
    if data is None:
        raise AuditIntegrityError("live dispatch lost its actual batch/provider owners")
    result = await self._primary_for_phase(runtime, inventory, selected)
    batch, accumulator, live = data.batch, data.accumulator, runtime.live
    if batch.phase != "call_ready" or batch.current_call is not None or batch.next_tool_index >= len(data.calls):
        raise AuditIntegrityError("live dispatch lost its actual unissued next call")
    context = await self._tool_context(runtime)
    call = ContinuationCallState(batch.next_tool_index, data.calls[batch.next_tool_index], batch.state, batch.state.version)
    batch.current_call, batch.phase = call, "call_active"
    preparation = await _prepare_continuation_call_live(
        call_model=result.outcome, ctx=context, llm_messages=live.llm_messages, batch=batch, call=call
    )
    issued_effect_key: str | None = None
    if preparation == "prepared":
        if call.audit is None:
            raise AuditIntegrityError("tool preparation lost its actual dispatch audit")
        # Prepared DATA precedes the SQL issue owner. The canonical writer
        # binds this locator to its actual checkpoint/event/physical fence;
        # it must never be injected into the pure prepared call codec.
        issued_effect_key = f"tool:{result.response.reference.digest}:{call.index}"
        self.runtime = runtime = replace(
            runtime,
            graph=runtime.graph.with_selected_dispatch(
                capture_dispatch_audit(call.audit, env_ref_names=inventory.names), context.tool_contract_dialect
            ),
        )
        captured = await capture_continuation_runtime_owners(runtime, selected=selected, inventory=inventory)
        top = captured.stack.frames[-1]
        if type(top) is not ToolBatchFrame:
            raise AuditIntegrityError("prepared tool capture changed its actual frame family")
        root = captured.stack.model_copy(
            update={
                "frames": (*captured.stack.frames[:-1], top.model_copy(update={"phase": "effect_reconciling"})),
                "effect_frontier": (
                    *captured.stack.effect_frontier,
                    EffectFrontierEntry(effect_id=issued_effect_key, disposition="dispatched", receipt_id=None),
                ),
            }
        )
        await self._retain(CapturedContinuationRuntime(selected, root, captured.chunks), inventory, "tool_issued")
        self.failure_custody = ContinuationToolFailureCustody(
            inventory.running, self.work, result.response, batch, data.calls, live.recorder, call
        )
        await _execute_continuation_call_live(
            call_model=result.outcome,
            ctx=context,
            acc=accumulator,
            llm_messages=live.llm_messages,
            batch=batch,
            call=call,
            failure_custody=self.failure_custody,
        )
    if preparation == "stop":
        batch.current_call, batch.phase = None, "dispatch_ready"
        return
    batch.next_tool_index += 1
    batch.current_call = None
    batch.phase = "dispatch_ready" if batch.stop_issued_batch or batch.next_tool_index == len(data.calls) else "call_ready"
    live.state, live.last_validation, live.last_runtime_preflight, live.advisor_calls_used = (
        batch.state,
        batch.last_validation,
        batch.last_runtime_preflight,
        batch.advisor_calls_used,
    )
    if batch.service_failure is not None:
        custody = self.failure_custody
        if preparation != "prepared" or custody is None or issued_effect_key is None or self.runtime is None:
            raise AuditIntegrityError("service completion lost its actual issued native phase")
        if self.terminal_completion is not None:
            raise AuditIntegrityError("service completion cannot replace another native terminal owner")
        try:
            self.terminal_completion = TerminalServiceCompletion(custody, self.runtime, context, result.outcome, issued_effect_key)
            # This failed batch cannot be encoded or restored. Its last valid
            # issued SQL cut is only a source bookmark for terminal P4 proof.
            await self._persist_batch(self.runtime.source.selected)
        except BaseException as error:
            combined = custody.failure(error)
            if combined is error:
                raise
            raise combined from combined.__cause__
        return
    if self.runtime is None:
        raise AuditIntegrityError("completed tool lost its retained runtime")
    runtime = self.runtime
    inventory = runtime.materialization.inventory
    selected = runtime.source.selected
    self.runtime = runtime = replace(runtime, graph=runtime.graph.without_selected_dispatch())
    captured = (
        await self._capture_issued_finish(runtime, selected=selected, inventory=inventory)
        if preparation == "prepared"
        else await capture_continuation_runtime_owners(runtime, selected=selected, inventory=inventory)
    )
    top = captured.stack.frames[-1]
    if type(top) is not ToolBatchFrame:
        raise AuditIntegrityError("completed tool capture changed its actual frame family")
    root = captured.stack.model_copy(update={"frames": (*captured.stack.frames[:-1], top.model_copy(update={"phase": "tool_ready"}))})
    if preparation == "prepared":
        if issued_effect_key is None:
            raise AuditIntegrityError("completed tool lost its actual issued effect locator")
        receipt = f"tool-result:{top.accumulator.digest}"
        root = root.model_copy(
            update={
                "effect_frontier": tuple(
                    EffectFrontierEntry(effect_id=entry.effect_id, disposition="committed", receipt_id=receipt)
                    if entry.effect_id == issued_effect_key
                    else entry
                    for entry in root.effect_frontier
                ),
                "frames": (
                    *root.frames[:-1],
                    root.frames[-1].model_copy(update={"settled_effect_ids": (*top.settled_effect_ids, issued_effect_key)}),
                ),
            }
        )
    await self._retain(
        CapturedContinuationRuntime(selected, root, captured.chunks),
        inventory,
        "tool_completed" if preparation == "prepared" else "tool_preparation_completed",
    )""",
    "initial._bind_first_state_on_connection": r'''def _bind_first_state_on_connection(
    conn: Connection,
    service: ComposerServiceImpl,
    running: ComposerOperationRunning,
    runtime: RestoredContinuationRuntime,
) -> RestoredContinuationRuntime:
    """Join the *proven first cut* to its already restored actual evidence state.

    This is not an equality-based general alias restoration. The real event,
    full forward transition, original references and absence of provider/batch
    progress are admitted on this current connection before the one assignment.
    """
    selected, inventory = runtime.source.selected, runtime.materialization.inventory
    source = read_composition_initialization_source_on_connection(conn, service, running, selected=selected, inventory=inventory)
    if not source.initialized or runtime.graph.batch is not None:
        raise AuditIntegrityError("first-state alias join requires its actual unconsumed initialization event")
    frame = selected.stack.frames[0]
    effects = runtime.graph.evidence.effects
    reader = runtime.graph.evidence.transcript._blob_reader
    if (
        type(frame) is not ComposeFrame
        or frame.initial_state != frame.state
        or effects.initial_state is not effects.latest_verified_state
        or canonical_json(effects.initial_state.to_dict()) != source.original.snapshot.initial_state_json
        or canonical_json(runtime.live.state.to_dict()) != source.original.snapshot.initial_state_json
        or runtime.live.turn_evidence is not runtime.graph.evidence
        or runtime.live.llm_messages is not runtime.graph.messages
        or runtime.live.recorder is not runtime.graph.recorder
        or runtime.bindings.session_operation_context is not running.session_operation_context
        or type(reader) is not CurrentTranscriptBlobReader
        or reader.service is not service
        or reader.context is not running.session_operation_context
    ):
        raise AuditIntegrityError("first-state alias join changed the actual current shared graph/reader")
    reader.require_for_evidence(runtime.graph.evidence)
    runtime.live.state = effects.initial_state
    from elspeth.web.sessions.continuation_composition_initialization import _read_composition_initialization_source_on_connection

    # Repeat EVERY actual current/original/event/TITLE SQL predicate after the
    # pure alias assignment. Only the first-value pure projection is shared:
    # its complete selected/chunk/original inputs must remain exactly equal
    # on this same connection; a changed metadata input refuses, never renews.
    after = _read_composition_initialization_source_on_connection(conn, service, running, selected=selected, inventory=inventory)
    if not source.same_original_source(after) or source.original != after.original:
        raise AuditIntegrityError("first-state alias changed its actual complete current source")
    return runtime
''',
    "initial._consume": r"""async def _consume(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: StoredContinuationSnapshot,
    work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner,
    progress: ComposerProgressSink | None,
) -> RestoredContinuationRuntime:
    running = work.running
    if type(running) is not ComposerOperationRunning:
        raise AuditIntegrityError("first Compose consumption lost its actual current running owner")
    complete = await load_selected_continuation(authority=authority, running=running, selected=selected, required_work=work)
    inventory = await materialize_continuation_env_inventory(
        advisor=service._advisor_checkpoint, authority=authority, selected=complete, required_work=work
    )
    runtime = await restore_continuation_runtime(
        service=service,
        authority=authority,
        selected=complete,
        inventory=inventory,
        required_work=work,
        provider_owner=provider_owner,
        progress=progress,
    )
    running = _work_owners(authority, inventory, work)

    def bind() -> RestoredContinuationRuntime:
        with locked_session_transaction(_sessions(service)._engine, str(running.claim.session_id)) as conn:
            return _bind_first_state_on_connection(conn, service, running, runtime)

    return await _registered(
        work=work, source=RequiredWorkSource.CONTINUATION_READ_SQL, operation=bind, expected=RestoredContinuationRuntime
    )
""",
    "initial.initialize_continuation_composition": r"""async def initialize_continuation_composition(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: StoredContinuationSnapshot,
    required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner,
    progress: ComposerProgressSink | None = None,
) -> RestoredContinuationRuntime:
    if (
        type(service) is not ComposerServiceImpl
        or type(authority) is not ComposerAsyncOperationAuthority
        or type(selected) is not StoredContinuationSnapshot
        or type(required_work) is not RequiredWorkBinding
        or type(provider_owner) is not ProviderInvocationOwner
        or provider_owner.required_work is not required_work
        or provider_owner.service is not _sessions(service)
        or _sessions(service)._engine is not authority._engine
    ):
        raise AuditIntegrityError("first Compose initialization requires actual service/SQL/provider-work owners")
    running = required_work.running
    custody = required_work.coordinator.authority
    if (
        type(running) is not ComposerOperationRunning
        or custody.authority_kind is not RequiredAuthorityKind.DURABLE_COMPOSE
        or custody.durable_operation_id != running.claim.operation_id
        or custody.claim_attempt != running.claim.attempt
    ):
        raise AuditIntegrityError("first Compose initialization changed its current required-work claim")
    required_work.validate_context(running.session_operation_context)
    root = decode_continuation_snapshot(selected.payload_json)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    clock = ContinuationActionClock(policy.actions.session_sql)
    clock.finish_preparation()

    async def prepare() -> PreparedCompositionInitialization | CurrentCompositionInitializationSource:
        return await _prepare(service=service, authority=authority, selected=selected, work=required_work)

    prepared = await clock.run("physical", prepare)

    async def retain_and_consume() -> RestoredContinuationRuntime:
        current = selected
        if type(prepared) is PreparedCompositionInitialization:
            current = await _registered(
                work=required_work,
                source=RequiredWorkSource.CONTINUATION_STATE_SQL,
                operation=lambda: authority.publish_composition_initialization(
                    running, composer=service, owners=prepared.owners, captured=prepared.captured
                ),
                expected=StoredContinuationSnapshot,
            )
        return await _consume(
            service=service, authority=authority, selected=current, work=required_work, provider_owner=provider_owner, progress=progress
        )

    runtime = await clock.run("retention", retain_and_consume)
    if type(prepared) is PreparedCompositionInitialization:
        await emit_progress(
            progress,
            ComposerProgressEvent(
                phase="starting",
                headline="I'm reading your request and current pipeline.",
                evidence=(
                    "The current pipeline state is prepared for the composer.",
                    "The pipeline composer skill pack and deployment overlay are included.",
                ),
                likely_next="ELSPETH will ask the model for the next safe pipeline action.",
            ),
        )
    return runtime
""",
    "driver._SegmentDriver.__init__": r"""def __init__(
    self, worker: ComposerAsyncWorker, services: ComposerAppServices, running: ComposerOperationRunning, lease: SessionOperationLease
):
    if type(services.composer_service) is not ComposerServiceImpl or type(lease.required_work) is not RequiredWorkCoordinator:
        raise AuditIntegrityError("V2 driver requires its actual Composer and required-work owners")
    if lease.context is not running.session_operation_context:
        raise AuditIntegrityError("V2 driver changed its exact started context")
    self.worker, self.services, self.running, self.lease = worker, services, running, lease
    self.service = services.composer_service
    self.authority = services.composer_operation_authority
    self.work = RequiredWorkBinding(lease.required_work, 0, 0, RequiredWorkRole.TURN, running)
    self.provider = ProviderInvocationOwner(service=services.session_service, required_work=self.work)
    self.phases = ContinuationLivePhases(self.service, self.authority, self.work, self.provider, lease)
    sql_deadline = lease.required_work.sql_deadline_owner
    if sql_deadline is None:
        raise AuditIntegrityError("V2 driver lost its actual SQL timing owner")
    self.sql_deadline = sql_deadline
    self._consumed_native: list[BaseException] = []
    self._committed_failure: CommittedContinuationFailure | None = None
""",
    "driver._SegmentDriver.advance": r"""async def advance(self, stored: StoredContinuationSnapshot, selected: SelectedContinuationState) -> MessageWithStateResponse | None:
    root, top = selected.stack, selected.stack.frames[-1]
    if type(top) is InitializeFrame:
        if top.ingress_phase == "reserved":
            if root.logical.kind != "compose_message":
                await self.service.advance_continuation_phase(
                    owner=self.phases, authority=self.authority, selected=selected, work=self.work, provider_owner=self.provider
                )
                return None
            request = SendMessageRequest.model_validate_json(top.request_json)
            sql, projection = self.work.reserve_pair(RequiredWorkSource.INGRESS_SQL, RequiredWorkSource.INGRESS_PROJECTION)
            try:
                ingress_result = await self.services.session_service.add_message_with_transcript(
                    self.running.claim.session_id,
                    "user",
                    request.content,
                    operation_id=UUID(self.running.claim.operation_id),
                    requested_state_id=request.state_id,
                    composition_state_id=request.state_id,
                    writer_principal="route_user_message",
                    session_operation_context=self.running.session_operation_context,
                    running=self.running,
                    required_work=sql,
                    continuation=stored,
                )
                if type(ingress_result) is not ContinuationIngressCheckpoint:
                    raise AuditIntegrityError("V2 ingress returned another actual owner")
            except BaseException as original:
                projection.complete_owned(original)
                raise
            projection.complete_owned()
            return None
        if top.ingress_phase == "inserted":
            await prepare_pretitle_bootstrap(service=self.service, authority=self.authority, selected=stored, required_work=self.work)
            return None
        if top.ingress_phase == "title_pending":
            if top.provider_effect_key is None:
                prepared = await prepare_current_title_provider(
                    service=self.service, authority=self.authority, selected=stored, required_work=self.work
                )
                await run_prepared_title_provider(prepared=prepared, invocation_owner=self.provider)
            else:
                current = await settle_current_title_result(service=self.service, running=self.running, required_work=self.work)
                await apply_current_title_success(service=self.service, selected=current.selected, required_work=self.work)
            return None
        runtime = await initialize_continuation_composition(
            service=self.service, authority=self.authority, selected=stored, required_work=self.work, provider_owner=self.provider
        )
        self.phases.bind_initial(runtime)
        return None
    if type(top) is ComposeFrame and top.phase == "action_ready":
        if self.phases.runtime is None:
            runtime = await initialize_continuation_composition(
                service=self.service, authority=self.authority, selected=stored, required_work=self.work, provider_owner=self.provider
            )
            self.phases.bind_initial(runtime)
        else:
            runtime, _inventory = await self.phases.current(selected)
        policy = runtime.source.policy
        # This packet owns delivery preflight only. The current SQL-backed
        # PRIMARY factory separately admits its original producer messages.
        _messages, _tools, delivery, _delivery_input = self.service._prepare_primary_delivery(
            messages=runtime.live.llm_messages,
            tools=runtime.live.tools,
            recorder=runtime.live.recorder,
            turn_evidence=runtime.live.turn_evidence,
            state=runtime.live.state,
            failed_turn=runtime.live.failed_turn,
        )
        primary_result = await run_continuation_primary(
            service=self.service,
            authority=self.authority,
            selected=runtime.source.selected,
            producers=runtime.materialization,
            messages=runtime.live.llm_messages,
            tools=runtime.live.tools,
            recorder=runtime.live.recorder,
            drained=runtime.drained,
            required_work=self.work,
            provider_owner=self.provider,
            timeout=policy.actions.primary.total,
        )
        if delivery is not None:
            runtime.live.turn_evidence.confirm_delivery(delivery, runtime.live.recorder.llm_calls)
        self.phases.accept_primary(primary_result)
        return None
    await self.service.advance_continuation_phase(
        owner=self.phases, authority=self.authority, selected=selected, work=self.work, provider_owner=self.provider
    )
    return None
""",
    "service.ComposerServiceImpl._dispatch_tool_batch": r'''async def _dispatch_tool_batch(
    self,
    *,
    call_model: _CallModelOutcome,
    mutation_ceiling: ComposerMutationCeiling | None,
    state: CompositionState,
    last_validation: ValidationSummary | None,
    last_runtime_preflight: ValidationResult | None,
    llm_messages: list[dict[str, Any]],
    recorder: BufferingRecorder,
    anti_anchor: AntiAnchorTracker,
    discovery_cache: dict[str, _CachedDiscoveryPayload],
    runtime_preflight_cache: _RuntimePreflightCache,
    session_id: str | None,
    session_operation_context: SessionOperationContext | None = None,
    user_id: str | None,
    user_message_id: str | None,
    user_message_content: str | None,
    current_state_id: str | None,
    actor: str,
    initial_version: int,
    deadline: float,
    progress: ComposerProgressSink | None,
    session_scope: str,
    advisor_calls_used: int,
    cancellation_requested: asyncio.Event,
    plugin_snapshot: PluginAvailabilitySnapshot,
    policy_catalog: PolicyCatalogView,
    composition_turns_used: int,
    discovery_turns_used: int,
    failed_turn: FailedTurnMetadata | None,
    provider_owner: ProviderInvocationOwner | None = None,
    turn_evidence: TurnEvidence | None = None,
    message_producers: CurrentContinuationMessageProducers | None = None,
) -> tuple[_DispatchOutcome, int]:
    """Phase P3 of the compose loop — delegates to :func:`tool_batch.run_tool_batch`."""
    from elspeth.web.composer.tool_batch import (
        BatchAccumulator,
        ToolBatchContext,
        ToolBatchCustody,
        ToolBatchProvenance,
        run_tool_batch,
    )

    turn_sessions_service = self._require_sessions_service() if session_id is not None else None
    turn_session_uuid = UUID(session_id) if session_id is not None else None
    turn_preferences = (
        await turn_sessions_service.get_composer_preferences(turn_session_uuid)
        if turn_sessions_service is not None and turn_session_uuid is not None
        else None
    )
    review_required = False
    if turn_session_uuid is not None:
        if type(mutation_ceiling) is not ComposerMutationCeiling or turn_preferences is None:
            raise AuditIntegrityError("Session mutation dispatch lost its request ceiling")
        if mutation_ceiling.session_id != str(turn_session_uuid):
            raise AuditIntegrityError("Mutation dispatch crossed sessions")
        if session_operation_context is not None:
            mutation_ceiling.validate_context(session_operation_context)
        if turn_evidence is not None and mutation_ceiling.request_id != turn_evidence.request_id:
            raise AuditIntegrityError("Mutation dispatch crossed logical requests")
        review_required = not mutation_ceiling.permits_auto(turn_preferences.trust_mode)
    elif mutation_ceiling is not None:
        raise AuditIntegrityError("Sessionless mutation dispatch received a mutation ceiling")
    ctx = ToolBatchContext(
        session_tools=self._session_tools,
        provenance=ToolBatchProvenance(
            model_identifier=self._model,
            provider=self._availability.provider,
            skill_hash=self._composer_skill_hash,
        ),
        custody=ToolBatchCustody(
            data_dir=self._data_dir,
            session_engine=self._session_engine,
            secret_service=self._secret_service,
            secret_wiring_policy=self._secret_wiring_policy,
            max_blob_storage_per_session_bytes=self._settings.max_blob_storage_per_session_bytes,
        ),
        redaction_telemetry=self._redaction_telemetry,
        preflight=self._preflight,
        schema_disclosure=self._schema_disclosure,
        advisor_checkpoint=self._advisor_checkpoint,
        provider_owner=provider_owner,
        advisor_max_calls_per_compose=self._settings.composer_advisor_max_calls_per_compose,
        advisor_timeout_seconds=self._settings.composer_advisor_timeout_seconds,
        recorder=recorder,
        anti_anchor=anti_anchor,
        discovery_cache=discovery_cache,
        runtime_preflight_cache=runtime_preflight_cache,
        session_id=session_id,
        session_operation_context=session_operation_context,
        user_id=user_id,
        user_message_id=user_message_id,
        user_message_content=user_message_content,
        current_state_id=current_state_id,
        actor=actor,
        initial_version=initial_version,
        deadline=deadline,
        progress=progress,
        session_scope=session_scope,
        turn_sessions_service=turn_sessions_service,
        turn_session_uuid=turn_session_uuid,
        turn_preferences=turn_preferences,
        mutation_ceiling=mutation_ceiling,
        review_required=review_required,
        cancellation_requested=cancellation_requested,
        plugin_snapshot=plugin_snapshot,
        policy_catalog=policy_catalog,
        tool_contract_dialect=self._planner_dialect,
        session_operation_authority=(turn_sessions_service.session_operation_authority if turn_sessions_service is not None else None),
        composition_turns_used=composition_turns_used,
        discovery_turns_used=discovery_turns_used,
        failed_turn=failed_turn,
        turn_evidence=turn_evidence,
        message_producers=message_producers,
    )
    acc = BatchAccumulator(
        state=state,
        last_validation=last_validation,
        last_runtime_preflight=last_runtime_preflight,
        advisor_calls_used=advisor_calls_used,
    )
    return await run_tool_batch(
        call_model=call_model,
        ctx=ctx,
        acc=acc,
        llm_messages=llm_messages,
    )
''',
    "service.ComposerServiceImpl._advance_live_compose_iteration": r"""async def _advance_live_compose_iteration(
    self,
    live: _ComposeIterationState,
    bindings: _ComposeIterationBindings,
) -> ComposerResult | None:
    state = live.state
    initial_version = live.initial_version
    llm_messages = live.llm_messages
    tools = live.tools
    recorder = live.recorder
    composition_turns_used = live.composition_turns_used
    discovery_turns_used = live.discovery_turns_used
    mutation_success_seen = live.mutation_success_seen
    discovery_cache = live.discovery_cache
    last_validation = live.last_validation
    runtime_preflight_cache = live.runtime_preflight_cache
    last_runtime_preflight = live.last_runtime_preflight
    anti_anchor = live.anti_anchor
    advisor_calls_used = live.advisor_calls_used
    repair_turns_used = live.repair_turns_used
    advisor_checkpoint_passes_used = live.advisor_checkpoint_passes_used
    advisor_review_state = live.advisor_review_state
    persisted_assistant_message_id = live.persisted_assistant_message_id
    persisted_assistant_content = live.persisted_assistant_content
    persisted_tool_call_turn = live.persisted_tool_call_turn
    failed_turn = live.failed_turn
    current_state_id = live.current_state_id
    advisor_repair_context_introduced = live.advisor_repair_context_introduced
    pending_advisor_elision_indices = live.pending_advisor_elision_indices
    turn_evidence = live.turn_evidence
    message = bindings.message
    session_id = bindings.session_id
    user_id = bindings.user_id
    user_message_id = bindings.user_message_id
    deadline = bindings.deadline
    progress = bindings.progress
    plugin_snapshot = bindings.plugin_snapshot
    policy_catalog = bindings.policy_catalog
    mutation_ceiling = bindings.mutation_ceiling
    session_operation_context = bindings.session_operation_context
    completion_gates = bindings.completion_gates
    diagnostics = bindings.diagnostics
    required_work = bindings.required_work
    provider_owner = bindings.provider_owner
    ingress = bindings.ingress
    # The bindings hold a frozen sequence; the audit path takes its own list.
    chat_ingress_inputs = None if bindings.chat_ingress_inputs is None else list(bindings.chat_ingress_inputs)
    actor = bindings.actor
    session_scope = bindings.session_scope
    for _continuation_iteration in (None,):
        try:
            # The compose-loop audit path captures the state id observed
            # before the provider call and passes this exact value to
            # persist_compose_turn_async as expected_current_state_id.
            call_model = await self._call_model_turn(
                llm_messages=llm_messages,
                tools=tools,
                state=state,
                initial_version=initial_version,
                deadline=deadline,
                recorder=recorder,
                progress=progress,
                message=message,
                composition_turns_used=composition_turns_used,
                discovery_turns_used=discovery_turns_used,
                failed_turn=failed_turn,
                provider_owner=provider_owner,
                turn_evidence=turn_evidence,
            )
            recovery_result = self._terminal_recovery_result(
                call_model.completion, state, recorder, prior_tools_persisted=persisted_tool_call_turn
            )
            if recovery_result is not None:
                return recovery_result
            # If no tool calls, the LLM is done — apply the final gate and return
            if not call_model.completion.tool_batch.calls:
                # A repair gate below may answer this reply with a user-role
                # injection and continue. The reply has to be in the provider
                # context first, or the next call shows a repair message
                # answering an assistant turn the model cannot see. Appended
                # BEFORE the gate so the gate's own
                # ``advisor_injection_index = len(llm_messages)`` stays exact,
                # and withdrawn again on ``action="return"``, where the reply
                # is published rather than superseded and the provider context
                # is left exactly as it was. A blank reply is skipped:
                # providers reject empty assistant content, and there are no
                # words to keep.
                superseded_reply = call_model.completion.message.content or ""
                superseded_reply_index: int | None = None
                if superseded_reply.strip():
                    superseded_reply_index = len(llm_messages)
                    llm_messages.append({"role": "assistant", "content": superseded_reply})
                terminate = await self._completion._try_terminate_no_tools(
                    assistant_message=call_model.completion.message,
                    turn_evidence=turn_evidence,
                    session_operation_context=session_operation_context,
                    message=message,
                    llm_messages=llm_messages,
                    state=state,
                    session_id=session_id,
                    current_state_id=current_state_id,
                    initial_version=initial_version,
                    user_id=user_id,
                    last_runtime_preflight=last_runtime_preflight,
                    runtime_preflight_cache=runtime_preflight_cache,
                    session_scope=session_scope,
                    mutation_success_seen=mutation_success_seen,
                    recorder=recorder,
                    progress=progress,
                    repair_turns_used=repair_turns_used,
                    persisted_assistant_message_id=persisted_assistant_message_id,
                    persisted_assistant_content=persisted_assistant_content,
                    persisted_tool_call_turn=persisted_tool_call_turn,
                    advisor_checkpoint_passes_used=advisor_checkpoint_passes_used,
                    plugin_snapshot=plugin_snapshot,
                    advisor_review_state=advisor_review_state,
                    deadline=deadline,
                    composition_turns_used=composition_turns_used,
                    discovery_turns_used=discovery_turns_used,
                    failed_turn=failed_turn,
                    completion_gates=completion_gates,
                    provider_owner=provider_owner,
                )
                if terminate.advisor_review_state is not None:
                    advisor_review_state = terminate.advisor_review_state
                if terminate.action == "return":
                    if superseded_reply_index is not None:
                        del llm_messages[superseded_reply_index]
                    # Offensive guard (explicit raise, not assert): ``python -O``
                    # strips assert statements. The contract between
                    # ``_dispatch_terminate_phase`` and this caller is that
                    # ``result`` is non-None whenever ``action == "return"``;
                    # a None here would be a compose-loop bug, not a recoverable
                    # state. Routed to the HTTP-500 static-detail handler at
                    # ``routes/composer.py:905`` via :class:`InvariantError`
                    # (B1-sanitised response body).
                    if terminate.result is None:
                        raise InvariantError(
                            "_dispatch_terminate_phase returned action='return' with result=None — "
                            "the terminate-phase contract requires result to be set whenever the "
                            "phase signals a return."
                        )
                    public_result = terminate.result
                    if advisor_repair_context_introduced:
                        public_result = await self._completion._qualified_advisor_repair_public_result(
                            public_result,
                            user_id=user_id,
                            session_id=session_id,
                            session_operation_context=session_operation_context,
                            cache=runtime_preflight_cache,
                            initial_version=initial_version,
                            session_scope=session_scope,
                            plugin_snapshot=plugin_snapshot,
                            publication_binding=terminate.publication_binding,
                            turn_evidence=turn_evidence,
                            user_message=message,
                        )
                    return self._completion._append_checkpoint_receipt(public_result, turn_evidence=turn_evidence)
                repair_turns_used += terminate.repair_turns_delta
                advisor_checkpoint_passes_used += terminate.advisor_passes_delta
                # The gate superseded this reply: it is never published, so keep
                # the model's words as a non-rendered audit row.
                if superseded_reply_index is not None:
                    await self._completion._persist_withheld_reply(
                        "repair_gate_superseded",
                        superseded_reply,
                        session_id=session_id,
                        session_operation_context=session_operation_context,
                    )
                if terminate.advisor_injection_index is not None:
                    advisor_repair_context_introduced = True
                    if _ELIDE_ADVISOR_EXCHANGE_AT_FINALIZE:
                        pending_advisor_elision_indices.append(terminate.advisor_injection_index)
                        # Elide the superseded reply WITH the advisor message it
                        # was answered by. Dropping only the injection would
                        # leave this reply directly before the repair turn's
                        # assistant tool-call message — two consecutive
                        # assistant messages, which some providers reject.
                        if superseded_reply_index is not None:
                            pending_advisor_elision_indices.append(superseded_reply_index)
                continue

            cancellation_requested = asyncio.Event()
            pre_batch_state = state

            async def _dispatch_and_persist_tool_turn(
                _call_model: _CallModelOutcome = call_model,
                _state: CompositionState = state,
                _last_validation: ValidationSummary | None = last_validation,
                _last_runtime_preflight: ValidationResult | None = last_runtime_preflight,
                _current_state_id: str | None = current_state_id,
                _advisor_calls_used: int = advisor_calls_used,
                _cancellation_requested: asyncio.Event = cancellation_requested,
                _persisted_tool_call_turn: bool = persisted_tool_call_turn,
                _persisted_assistant_message_id: str | None = persisted_assistant_message_id,
                _persisted_assistant_content: str | None = persisted_assistant_content,
                _advisor_repair_context_introduced: bool = advisor_repair_context_introduced,
                _composition_turns_used: int = composition_turns_used,
                _discovery_turns_used: int = discovery_turns_used,
                _failed_turn: FailedTurnMetadata | None = failed_turn,
            ) -> tuple[_DispatchOutcome, _PersistOutcome, int, bool, bool, BaseException | None]:
                invocation_start = len(recorder.invocations)
                try:
                    dispatch_result, updated_advisor_calls_used = await self._dispatch_tool_batch(
                        call_model=_call_model,
                        mutation_ceiling=mutation_ceiling,
                        session_operation_context=session_operation_context,
                        state=_state,
                        last_validation=_last_validation,
                        last_runtime_preflight=_last_runtime_preflight,
                        llm_messages=llm_messages,
                        recorder=recorder,
                        anti_anchor=anti_anchor,
                        discovery_cache=discovery_cache,
                        runtime_preflight_cache=runtime_preflight_cache,
                        session_id=session_id,
                        user_id=user_id,
                        user_message_id=user_message_id,
                        user_message_content=message,
                        current_state_id=_current_state_id,
                        actor=actor,
                        initial_version=initial_version,
                        deadline=deadline,
                        progress=progress,
                        session_scope=session_scope,
                        advisor_calls_used=_advisor_calls_used,
                        cancellation_requested=_cancellation_requested,
                        plugin_snapshot=plugin_snapshot,
                        policy_catalog=policy_catalog,
                        composition_turns_used=_composition_turns_used,
                        discovery_turns_used=_discovery_turns_used,
                        failed_turn=_failed_turn,
                        provider_owner=provider_owner,
                        turn_evidence=turn_evidence,
                    )
                except ComposerRuntimePreflightError as preflight_failure:
                    raise ComposerRuntimePreflightError(
                        original_exc=preflight_failure.original_exc,
                        partial_state=preflight_failure.partial_state,
                        tool_invocations=recorder.invocations[invocation_start:],
                        llm_calls=preflight_failure.llm_calls,
                        failed_turn=preflight_failure.failed_turn,
                    ) from preflight_failure.original_exc
                except (AssertionError, MemoryError, RecursionError, SystemError, AuditIntegrityError) as fatal_failure:
                    current_invocations = recorder.invocations[invocation_start:]
                    if not current_invocations:
                        raise
                    raise ComposerToolFailure(
                        fatal_failure,
                        tool_invocations=current_invocations,
                        llm_calls=recorder.llm_calls,
                    ) from fatal_failure
                except asyncio.CancelledError as cancellation:
                    # P4 has not started. Earlier loop turns are already durable
                    # and must never be rebound by this batch's fallback drain.
                    raise ComposerToolCancellation(
                        cancellation,
                        tool_invocations=recorder.invocations[invocation_start:],
                        llm_calls=recorder.llm_calls,
                    ) from cancellation
                if diagnostics is not None:
                    diagnostics.tool_outcomes = dispatch_result.tool_outcomes
                    diagnostics.pre_state_id = dispatch_result.pre_state_id

                # Do not start a new advisory call after cancellation. If an
                # advisory call was already running when cancellation landed,
                # the enclosing shield lets it finish and P4 still publishes
                # the completed audit prefix.
                early_advisor_message_count = len(llm_messages)
                early_checkpoint_deadline_expired = False
                early_checkpoint_failure: BaseException | None = None
                try:
                    if (
                        not _cancellation_requested.is_set()
                        and dispatch_result.advisor_compose_timeout is None
                        and dispatch_result.advisor_failure is None
                        and dispatch_result.service_failure is None
                        and dispatch_result.authority_revocation is None
                    ):
                        try:
                            await self._advisor_checkpoint._maybe_run_early_checkpoint(
                                state=dispatch_result.state,
                                prev_state=_state,
                                session_id=session_id,
                                llm_messages=llm_messages,
                                recorder=recorder,
                                progress=progress,
                                deadline=deadline,
                                session_operation_context=session_operation_context,
                                provider_owner=provider_owner,
                                turn_evidence=turn_evidence,
                            )
                        except _AdvisorCheckpointComposeDeadlineExpired:
                            # The driver converts this signal after persistence
                            # and after plugin/cancellation primacy checks.
                            early_checkpoint_deadline_expired = True
                        except (Exception, asyncio.CancelledError, BaseExceptionGroup) as checkpoint_failure:
                            # Defer the exact failure until P4's committed
                            # metadata has reached the driver. This is cleanup
                            # custody, not classification as a provider outage.
                            early_checkpoint_failure = checkpoint_failure
                finally:
                    # The tool batch already ran. Its audit must be persisted
                    # even when an internal advisor failure unwinds this turn.
                    # A successful tool remains successful; the separate
                    # checkpoint failure propagates after this mandatory work.
                    early_advisor_context_introduced = len(llm_messages) > early_advisor_message_count
                    turn_evidence.transcript.observe_messages(llm_messages)
                    try:
                        persist_result = await self._persist_turn_audit(
                            tool_outcomes=dispatch_result.tool_outcomes,
                            mutation_ceiling=mutation_ceiling,
                            authority_revocation=dispatch_result.authority_revocation,
                            base_state=_state,
                            decoded_args_by_call_id=dispatch_result.decoded_args_by_call_id,
                            private_authorings=dispatch_result.private_authorings,
                            private_invocations=dispatch_result.private_invocations,
                            assistant_message=dispatch_result.assistant_message,
                            raw_assistant_content=dispatch_result.raw_assistant_content,
                            assistant_tool_calls=dispatch_result.assistant_tool_calls,
                            crash_pending=(
                                dispatch_result.plugin_crash is not None
                                or dispatch_result.advisor_failure is not None
                                or dispatch_result.service_failure is not None
                            ),
                            session_id=session_id,
                            session_operation_context=session_operation_context,
                            current_state_id=_current_state_id,
                            persisted_tool_call_turn=_persisted_tool_call_turn,
                            persisted_assistant_message_id=_persisted_assistant_message_id,
                            persisted_assistant_content=_persisted_assistant_content,
                            ingress=ingress,
                            chat_ingress_inputs=chat_ingress_inputs,
                            advisor_repair_context_introduced=_advisor_repair_context_introduced,
                            required_work=required_work,
                            turn_evidence=turn_evidence,
                        )
                    except BaseException as p4_direct_failure:
                        if dispatch_result.service_failure is not None:
                            from elspeth.web.composer.continuation_tool_failure import combine_failure_objects

                            # A thrown await does not establish rollback or
                            # authorize an audit replay. Keep both original
                            # objects and their prior causes for the real
                            # publication/operation owner to classify.
                            combined = combine_failure_objects(p4_direct_failure, dispatch_result.service_failure)
                            raise combined from combined.__cause__
                        if not isinstance(p4_direct_failure, (AuditIntegrityError, IntegrityError)):
                            raise
                        refused = dispatch_result.advisor_failure
                        if not isinstance(refused, ComposerContextUnsupported):
                            raise
                        if isinstance(p4_direct_failure, IntegrityError):
                            # The SQL transaction owner rethrows this native
                            # constraint failure only after rollback. Retain
                            # that exact SQL error under integrity primacy.
                            current_publication_failure = AuditIntegrityError(
                                "Advisor failure audit row could not be persisted",
                                failed_turn=FailedTurnMetadata(
                                    assistant_message_id=None,
                                    tool_calls_attempted=len(dispatch_result.assistant_tool_calls),
                                    tool_responses_persisted=0,
                                ),
                            )
                            previous_cause: BaseException | None = p4_direct_failure
                            unpublished = True
                        else:
                            current_publication_failure = p4_direct_failure
                            previous_cause = p4_direct_failure.__cause__
                            failed_publication = p4_direct_failure.failed_turn
                            unpublished = (
                                isinstance(previous_cause, SQLAlchemyError)
                                and type(failed_publication) is FailedTurnMetadata
                                and failed_publication.assistant_message_id is None
                                and failed_publication.tool_responses_persisted == 0
                            )
                        current_publication_failure.__cause__ = (
                            refused
                            if previous_cause is None
                            else BaseExceptionGroup("Advisor refusal and original P4 failure", (refused, previous_cause))
                        )
                        if not unpublished:
                            # A throw alone does not prove rollback. In
                            # particular, never replay a committed P4 after
                            # a later outcome/projection validation failure,
                            # or infer rollback from forensic metadata alone.
                            raise
                        # Native SQL failure plus the P4 owner's zero-row
                        # disposition preserve the exact current suffix for
                        # the existing audit-only recovery consumer.
                        current_invocations = recorder.invocations[invocation_start:]
                        if tuple(item.tool_call_id for item in current_invocations) != tuple(
                            outcome.call.id for outcome in dispatch_result.tool_outcomes
                        ):
                            raise InvariantError(
                                "Advisor publication failure lost its exact current invocation cohort"
                            ) from p4_direct_failure
                        raise ComposerToolFailure(
                            current_publication_failure,
                            tool_invocations=current_invocations,
                            llm_calls=recorder.llm_calls,
                        ) from current_publication_failure
                    if diagnostics is not None:
                        diagnostics.redacted_assistant_tool_calls = persist_result.redacted_assistant_tool_calls
                        diagnostics.redacted_tool_rows = persist_result.redacted_tool_rows
                        diagnostics.audit_outcome = persist_result.audit_outcome
                return (
                    dispatch_result,
                    persist_result,
                    updated_advisor_calls_used,
                    early_advisor_context_introduced,
                    early_checkpoint_deadline_expired,
                    early_checkpoint_failure,
                )

            (
                (
                    dispatch,
                    persist,
                    advisor_calls_used,
                    early_advisor_context_introduced,
                    early_checkpoint_deadline_expired,
                    early_checkpoint_failure,
                ),
                deferred_cancel,
            ) = await _await_tool_turn_with_deferred_cancellation(
                _dispatch_and_persist_tool_turn(),
                cancellation_requested=cancellation_requested,
            )
            if early_advisor_context_introduced:
                advisor_repair_context_introduced = True
            # State the driver still owns across iterations updates from
            # the dispatch carrier; persist + classify consume the rest
            # of the dispatch fields directly.
            state = dispatch.state if persist.authority_revocation is None else pre_batch_state
            if persist.authority_revocation is None:
                last_validation = dispatch.last_validation
                last_runtime_preflight = dispatch.last_runtime_preflight
                if dispatch.mutation_success_observed:
                    mutation_success_seen = True
                advisor_review_state = _record_advisor_repair_mutations(
                    advisor_review_state,
                    dispatch.tool_outcomes,
                )
            current_state_id = persist.current_state_id
            persisted_assistant_message_id = persist.persisted_assistant_message_id
            persisted_assistant_content = persist.persisted_assistant_content
            persisted_tool_call_turn = persist.persisted_tool_call_turn
            failed_turn = persist.failed_turn
            pending_checkpoint_failures = tuple(
                failure for failure in (early_checkpoint_failure, persist.effect_verification_failure) if failure is not None
            )
            # Finalize-context elision drain (Task 6 Step 3). Gated on
            # ``dispatch.mutation_success_observed`` — NOT merely "a tool-call
            # turn dispatched" — because a discovery-only turn (get_plugin_schema,
            # preview_pipeline, list_*) or an all-ARG_ERROR turn makes tool
            # calls without repairing anything. Draining on those would wipe
            # the advisor findings from context before any fix landed, so the
            # model's next no-tool reply "repairs" nothing, the END gate
            # re-flags on unchanged state, and the run needlessly blocks
            # (review finding 1). Only a turn that actually mutated state
            # counts as the repair the model was asked for.
            #
            # Residual (documented, not closed): a repair spanning TWO
            # mutating turns (e.g. a discovery turn to inspect the schema,
            # then the mutating fix on the turn after) still loses the
            # advisor text after the FIRST mutating turn, even though the
            # second mutating turn is still part of the same repair attempt.
            # Narrowed to the common single-mutating-turn case, not closed
            # for the general multi-turn repair case.
            if pending_advisor_elision_indices and dispatch.mutation_success_observed and persist.authority_revocation is None:
                # Interleaved-turn boundary (review finding 3): a turn that
                # emits BOTH prose and tool_calls keeps that prose verbatim in
                # the appended assistant message (tool_batch.py) — elision
                # only removes the injected advisor message itself, never the
                # model's own reasoning/rebuttal prose from an interleaved
                # turn. Deliberate: reasoning continuity for the model's own
                # words outweighs a second-order anchoring risk that the
                # Steps 1-2 output-contract clause already covers.
                for elision_index in sorted(pending_advisor_elision_indices, reverse=True):
                    del llm_messages[elision_index]
                pending_advisor_elision_indices = []
            if dispatch.service_failure is not None:
                from elspeth.web.composer.continuation_tool_failure import combine_failure_objects

                companions: list[BaseException] = list(pending_checkpoint_failures)
                if deferred_cancel is not None:
                    companions.append(deferred_cancel)
                if persist.unwind_audit_failed:
                    publication_failure = AuditIntegrityError(
                        "Service failure audit row could not be persisted", failed_turn=failed_turn
                    )
                    combined = combine_failure_objects(publication_failure, dispatch.service_failure, *companions)
                    if not isinstance(combined, Exception):
                        raise combined from combined.__cause__
                    # This explicit returned disposition owns an unpublished
                    # current suffix; do not include prior committed turns.
                    raise ComposerToolFailure(
                        combined, tool_invocations=dispatch.private_invocations, llm_calls=recorder.llm_calls
                    ) from combined
                combined = combine_failure_objects(dispatch.service_failure, *companions)
                raise combined from combined.__cause__

            if dispatch.plugin_crash is not None:
                # Plugin-crash propagation discipline (plan §5.7): the
                # capture in P3 already snapshotted `state` after every
                # prior successful tool-call mutation. If persistence
                # succeeded, re-capture with the post-persist failed_turn
                # so the route layer's _handle_plugin_crash sees the
                # complete partial-state story; otherwise raise the
                # original capture as-is.
                if persisted_tool_call_turn:
                    persisted_plugin_crash = ComposerPluginCrashError.capture(
                        dispatch.plugin_crash.original_exc,
                        state=state,
                        initial_version=initial_version,
                        tool_invocations=(),
                        llm_calls=recorder.llm_calls,
                        failed_turn=failed_turn,
                    )
                    if dispatch.plugin_crash_cause is None:
                        if pending_checkpoint_failures:
                            raise persisted_plugin_crash from BaseExceptionGroup(
                                "Tool failure retained checkpoint failures", pending_checkpoint_failures
                            )
                        raise persisted_plugin_crash
                    if pending_checkpoint_failures:
                        raise persisted_plugin_crash from BaseExceptionGroup(
                            "Tool failure retained checkpoint failures",
                            (dispatch.plugin_crash_cause, *pending_checkpoint_failures),
                        )
                    raise persisted_plugin_crash from dispatch.plugin_crash_cause
                if persist.unwind_audit_failed:
                    # P4 rolled back. Preserve only this unpersisted turn's
                    # suffix: recorder.invocations is request-cumulative, so
                    # carrying the full buffer after an earlier successful P4
                    # would duplicate already committed rows when the route
                    # drains the crash.
                    current_invocation_count = len(dispatch.tool_outcomes)
                    if current_invocation_count == 0 or current_invocation_count > len(dispatch.plugin_crash.tool_invocations):
                        raise InvariantError("plugin crash dispatch must carry one invocation per current-turn tool outcome")
                    unpersisted_plugin_crash = ComposerPluginCrashError.capture(
                        dispatch.plugin_crash.original_exc,
                        state=state,
                        initial_version=initial_version,
                        tool_invocations=dispatch.plugin_crash.tool_invocations[-current_invocation_count:],
                        llm_calls=recorder.llm_calls,
                        failed_turn=failed_turn,
                    )
                    if dispatch.plugin_crash_cause is None:
                        if pending_checkpoint_failures:
                            raise unpersisted_plugin_crash from BaseExceptionGroup(
                                "Tool failure retained checkpoint failures", pending_checkpoint_failures
                            )
                        raise unpersisted_plugin_crash
                    if pending_checkpoint_failures:
                        raise unpersisted_plugin_crash from BaseExceptionGroup(
                            "Tool failure retained checkpoint failures",
                            (dispatch.plugin_crash_cause, *pending_checkpoint_failures),
                        )
                    raise unpersisted_plugin_crash from dispatch.plugin_crash_cause
                # No session/audit target was configured, so nothing in the
                # request-cumulative crash carrier has been persisted yet.
                if dispatch.plugin_crash_cause is None:
                    if pending_checkpoint_failures:
                        raise dispatch.plugin_crash from BaseExceptionGroup(
                            "Tool failure retained checkpoint failures", pending_checkpoint_failures
                        )
                    raise dispatch.plugin_crash
                if pending_checkpoint_failures:
                    raise dispatch.plugin_crash from BaseExceptionGroup(
                        "Tool failure retained checkpoint failures",
                        (dispatch.plugin_crash_cause, *pending_checkpoint_failures),
                    )
                raise dispatch.plugin_crash from dispatch.plugin_crash_cause

            if dispatch.advisor_failure is not None:
                # The advisor boundary deliberately preserves unclassified
                # controlled-code faults instead of laundering them into a
                # provider outage. P3 closed the current invocation, and P4
                # reported its publication disposition. Failed publication
                # is a Tier-1 integrity fault that takes primacy while the
                # audit-only recovery owner retains unpublished evidence.
                if persist.unwind_audit_failed:
                    publication_failure = AuditIntegrityError(
                        "Advisor failure audit row could not be persisted",
                        failed_turn=failed_turn,
                    )
                    if isinstance(dispatch.advisor_failure, ComposerContextUnsupported):
                        # P4 explicitly rolled back this issued batch. The
                        # recorder is request-cumulative, so recover only
                        # the current dispatch's original invocation cohort.
                        # The audit-only route consumer must never replay a
                        # previous successful P4 or save rolled-back state.
                        current_invocations = dispatch.private_invocations
                        if tuple(item.tool_call_id for item in current_invocations) != tuple(
                            outcome.call.id for outcome in dispatch.tool_outcomes
                        ):
                            raise InvariantError("Advisor rollback lost its exact current invocation cohort")
                        retained_failures: tuple[BaseException, ...] = (dispatch.advisor_failure,)
                        if persist.effect_verification_failure is not None:
                            retained_failures += (persist.effect_verification_failure,)
                        if deferred_cancel is not None:
                            retained_failures += (deferred_cancel,)
                        publication_failure.__cause__ = (
                            retained_failures[0]
                            if len(retained_failures) == 1
                            else BaseExceptionGroup("Advisor refusal and P4 rollback outcomes", retained_failures)
                        )
                        raise ComposerToolFailure(
                            publication_failure,
                            tool_invocations=current_invocations,
                            llm_calls=recorder.llm_calls,
                        ) from publication_failure
                    raise publication_failure from dispatch.advisor_failure
                if isinstance(dispatch.advisor_failure, (PromptGuardRefused, ComposerContextUnsupported)):
                    advisor_refusal = dispatch.advisor_failure.with_evidence(
                        state=state,
                        initial_version=state.version,
                        tool_invocations=() if persisted_tool_call_turn else recorder.invocations,
                        llm_calls=recorder.llm_calls,
                        failed_turn=failed_turn,
                    )
                    advisor_refusal.__cause__ = dispatch.advisor_failure
                    if deferred_cancel is not None and isinstance(advisor_refusal, ComposerContextUnsupported):
                        # Required P4 work has completed, but an input
                        # refusal cannot consume the caller's cancellation.
                        # Keep the refusal and required-effect failure in
                        # its cause, where integrity reduction still wins.
                        attach_llm_calls(deferred_cancel, recorder)
                        cancellation_causes: tuple[BaseException, ...] = (advisor_refusal,)
                        if persist.effect_verification_failure is not None:
                            cancellation_causes += (persist.effect_verification_failure,)
                        if deferred_cancel.__cause__ is not None:
                            cancellation_causes += (deferred_cancel.__cause__,)
                        raise deferred_cancel from BaseExceptionGroup(
                            "Manual Advisor context refusal and deferred cancellation outcomes", cancellation_causes
                        )
                    if persist.effect_verification_failure is not None:
                        advisor_refusal.__cause__ = dispatch.advisor_failure
                        raise BaseExceptionGroup(
                            "Manual advisor guard refusal and committed effect verification failure",
                            (advisor_refusal, persist.effect_verification_failure),
                        ) from None
                    raise advisor_refusal from dispatch.advisor_failure
                if persist.effect_verification_failure is not None:
                    prior_cause = dispatch.advisor_failure.__cause__
                    raise dispatch.advisor_failure from BaseExceptionGroup(
                        "Advisor failure and committed effect verification failure",
                        (persist.effect_verification_failure,)
                        if prior_cause is None
                        else (prior_cause, persist.effect_verification_failure),
                    )
                raise dispatch.advisor_failure

            if deferred_cancel is not None:
                # P3's in-flight tool and P4's atomic audit publication are
                # now complete. Preserve any LLM-call audit carried by this
                # compose request, then resume the original cancellation at
                # the first safe checkpoint before P5 or another model turn.
                attach_llm_calls(deferred_cancel, recorder)
                if pending_checkpoint_failures:
                    prior_cause = deferred_cancel.__cause__
                    raise deferred_cancel from BaseExceptionGroup(
                        "Caller cancellation retained checkpoint failures",
                        pending_checkpoint_failures if prior_cause is None else (prior_cause, *pending_checkpoint_failures),
                    )
                raise deferred_cancel

            if early_checkpoint_failure is not None:
                if isinstance(early_checkpoint_failure, (PromptGuardRefused, ComposerContextUnsupported)):
                    early_refusal = early_checkpoint_failure.with_evidence(
                        state=state,
                        initial_version=state.version,
                        tool_invocations=() if persisted_tool_call_turn else recorder.invocations,
                        llm_calls=recorder.llm_calls,
                        failed_turn=failed_turn,
                    )
                    if persist.effect_verification_failure is not None:
                        # P4 already committed. Keep both originals visible to
                        # the operation reducer so a policy refusal cannot mask
                        # failure to verify the saved effect or replay its rows.
                        early_refusal.__cause__ = early_checkpoint_failure
                        raise BaseExceptionGroup(
                            "EARLY guard refusal and committed effect verification failure",
                            (early_refusal, persist.effect_verification_failure),
                        ) from None
                    raise early_refusal from early_checkpoint_failure
                attach_llm_calls(early_checkpoint_failure, recorder)
                if persist.effect_verification_failure is not None:
                    prior_cause = early_checkpoint_failure.__cause__
                    raise early_checkpoint_failure from BaseExceptionGroup(
                        "EARLY checkpoint failure retained committed effect verification failure",
                        (persist.effect_verification_failure,)
                        if prior_cause is None
                        else (prior_cause, persist.effect_verification_failure),
                    )
                raise early_checkpoint_failure

            if dispatch.advisor_compose_timeout is not None:
                current_invocation_count = len(dispatch.tool_outcomes)
                if current_invocation_count == 0 or current_invocation_count > len(recorder.invocations):
                    raise InvariantError("advisor timeout dispatch must carry one invocation per current-turn tool outcome")
                advisor_timeout = ComposerConvergenceError.capture(
                    max_turns=composition_turns_used + discovery_turns_used,
                    budget_exhausted="timeout",
                    state=state,
                    initial_version=initial_version,
                    # A successful P4 made the request's accumulated trail
                    # durable, so the route must not replay it. Without a
                    # persistence target, none of the prior turns is durable:
                    # preserve the recorder's complete in-memory trail.
                    tool_invocations=() if persisted_tool_call_turn else recorder.invocations,
                    llm_calls=recorder.llm_calls,
                    failed_turn=failed_turn,
                )
                if persist.effect_verification_failure is not None:
                    raise advisor_timeout from persist.effect_verification_failure
                raise advisor_timeout

            if early_checkpoint_deadline_expired:
                charged_composition_turns = composition_turns_used + (1 if dispatch.turn_has_mutation else 0)
                charged_discovery_turns = discovery_turns_used + (
                    1 if dispatch.turn_has_discovery and not dispatch.turn_has_mutation else 0
                )
                early_timeout = ComposerConvergenceError.capture(
                    max_turns=charged_composition_turns + charged_discovery_turns,
                    budget_exhausted="timeout",
                    state=state,
                    initial_version=initial_version,
                    tool_invocations=() if persisted_tool_call_turn else recorder.invocations,
                    llm_calls=recorder.llm_calls,
                    failed_turn=failed_turn,
                )
                if persist.effect_verification_failure is not None:
                    raise early_timeout from persist.effect_verification_failure
                raise early_timeout

            if persist.effect_verification_failure is not None:
                attach_llm_calls(persist.effect_verification_failure, recorder)
                raise persist.effect_verification_failure

            if persist.authority_revocation is not None:
                raise ComposerMutationAuthorityRevoked(
                    revocation=persist.authority_revocation,
                    llm_calls=recorder.llm_calls,
                    failed_turn=failed_turn,
                    state=state,
                    initial_version=initial_version,
                )

            classify = await self._classify_and_budget_turn(
                dispatch=dispatch,
                turn_evidence=turn_evidence,
                session_operation_context=session_operation_context,
                persist=persist,
                llm_messages=llm_messages,
                tools=tools,
                recorder=recorder,
                anti_anchor=anti_anchor,
                progress=progress,
                message=message,
                initial_version=initial_version,
                deadline=deadline,
                runtime_preflight_cache=runtime_preflight_cache,
                session_scope=session_scope,
                session_id=session_id,
                user_id=user_id,
                mutation_success_seen=mutation_success_seen,
                repair_turns_used=repair_turns_used,
                composition_turns_used=composition_turns_used,
                discovery_turns_used=discovery_turns_used,
                advisor_checkpoint_passes_used=advisor_checkpoint_passes_used,
                plugin_snapshot=plugin_snapshot,
                advisor_review_state=advisor_review_state,
                completion_gates=completion_gates,
                provider_owner=provider_owner,
            )
            composition_turns_used += classify.composition_turns_delta
            discovery_turns_used += classify.discovery_turns_delta
            advisor_checkpoint_passes_used += classify.advisor_passes_delta
            repair_turns_used += classify.repair_turns_delta
            if classify.action == "return":
                # Offensive guard (explicit raise, not assert): ``python -O``
                # strips assert statements. The contract between
                # ``_dispatch_classify_phase`` and this caller is that
                # ``result`` is non-None whenever ``action == "return"``
                # (the B-4D-3 last-chance branch sets it). A None here would
                # be a compose-loop bug. Routed to the HTTP-500 static-detail
                # handler at ``routes/composer.py:905`` via
                # :class:`InvariantError` (B1-sanitised response body).
                if classify.result is None:
                    raise InvariantError(
                        "_dispatch_classify_phase returned action='return' with result=None — "
                        "the classify-phase contract requires result to be set whenever the "
                        "phase signals a return."
                    )
                if classify.result.graph_recovery_intent is not None:
                    return classify.result
                public_result = classify.result
                if advisor_repair_context_introduced:
                    public_result = await self._completion._qualified_advisor_repair_public_result(
                        public_result,
                        user_id=user_id,
                        session_id=session_id,
                        session_operation_context=session_operation_context,
                        cache=runtime_preflight_cache,
                        initial_version=initial_version,
                        session_scope=session_scope,
                        plugin_snapshot=plugin_snapshot,
                        publication_binding=classify.publication_binding,
                        turn_evidence=turn_evidence,
                        user_message=message,
                    )
                return self._completion._append_checkpoint_receipt(public_result, turn_evidence=turn_evidence)
            continue
        finally:
            live.state = state
            live.initial_version = initial_version
            live.llm_messages = llm_messages
            live.tools = tools
            live.recorder = recorder
            live.composition_turns_used = composition_turns_used
            live.discovery_turns_used = discovery_turns_used
            live.mutation_success_seen = mutation_success_seen
            live.discovery_cache = discovery_cache
            live.last_validation = last_validation
            live.runtime_preflight_cache = runtime_preflight_cache
            live.last_runtime_preflight = last_runtime_preflight
            live.anti_anchor = anti_anchor
            live.advisor_calls_used = advisor_calls_used
            live.repair_turns_used = repair_turns_used
            live.advisor_checkpoint_passes_used = advisor_checkpoint_passes_used
            live.advisor_review_state = advisor_review_state
            live.persisted_assistant_message_id = persisted_assistant_message_id
            live.persisted_assistant_content = persisted_assistant_content
            live.persisted_tool_call_turn = persisted_tool_call_turn
            live.failed_turn = failed_turn
            live.current_state_id = current_state_id
            live.advisor_repair_context_introduced = advisor_repair_context_introduced
            live.pending_advisor_elision_indices = pending_advisor_elision_indices
            live.turn_evidence = turn_evidence
    return None""",
    "phases.ContinuationLivePhases": r'''@dataclass(slots=True)
class ContinuationLivePhases:
    service: ComposerServiceImpl
    authority: ComposerAsyncOperationAuthority
    work: RequiredWorkBinding
    provider_owner: ProviderInvocationOwner
    lease: SessionOperationLease
    runtime: RestoredContinuationRuntime | None = None
    primary: PrimaryProviderResult | RetainedPrimaryPhaseData | None = None
    p4: ContinuationLiveP4 | None = None
    latest_snapshot: StoredContinuationSnapshot | None = None
    cancellation_requested: asyncio.Event = field(default_factory=asyncio.Event)
    business_stage: ContinuationStage[None] | None = None
    failure_custody: ContinuationToolFailureCustody | None = None
    terminal_completion: TerminalServiceCompletion | None = None

    def observe_stop(self) -> None:
        self.cancellation_requested.set()

    def cancel_business(self, reason: ComposerOperationCancel) -> None:
        self.observe_stop()
        stage = self.business_stage
        if stage is not None and stage.task is not None and not stage.task.done():
            stage.task.cancel(reason)

    async def _tool_context(self, runtime: RestoredContinuationRuntime) -> ToolBatchContext:
        context = await build_runtime_tool_batch_context(runtime, cancellation_requested=self.cancellation_requested)
        owner = self.work.coordinator.sql_deadline_owner
        if owner is not None:
            if owner.running is not runtime.materialization.inventory.running:
                raise AuditIntegrityError("tool context changed its actual original timing owner")
            now = time.monotonic()
            remaining = owner.remaining_dispatch(monotonic_now=now)
            if remaining <= 0:
                raise TimeoutError("tool action exhausted its original dispatch allowance")
            context = replace(context, deadline=min(context.deadline, now + remaining))
        return context

    def bind_initial(self, runtime: RestoredContinuationRuntime) -> None:
        if (
            self.runtime is not None
            or type(runtime) is not RestoredContinuationRuntime
            or runtime.materialization.service is not self.service
            or runtime.materialization.authority is not self.authority
            or runtime.bindings.required_work is not self.work
            or runtime.bindings.provider_owner is not self.provider_owner
        ):
            raise AuditIntegrityError("live phase initialization changed its actual runtime owners")
        self.runtime = runtime

    def accept_primary(self, result: PrimaryProviderResult) -> None:
        if type(result) is not PrimaryProviderResult or type(result.outcome) is not _CallModelOutcome:
            raise AuditIntegrityError("live phases lost their actual provider result")
        self.primary = result

    async def _primary_for_phase(
        self, runtime: RestoredContinuationRuntime, inventory: CurrentContinuationEnvInventory, selected: SelectedContinuationState
    ) -> PrimaryProviderResult | RetainedPrimaryPhaseData:
        compose = tuple(frame for frame in selected.stack.frames if type(frame) is ComposeFrame)
        if len(compose) != 1 or compose[0].pending_response is None:
            raise AuditIntegrityError("live phase lost its actual selected paid response")
        if self.primary is None:
            retained = await _registered_read(
                self.work,
                RetainedPrimaryPhaseData,
                partial(
                    self.authority.read_continuation_phase_primary,
                    inventory.running,
                    selected=selected,
                    reference=compose[0].pending_response,
                    env_ref_names=inventory.names,
                ),
            )
            audit = retained.outcome.audit_call
            if audit is None:
                raise AuditIntegrityError("cold paid phase lost its actual physical audit DATA")
            matching = tuple(call for call in runtime.live.recorder.llm_calls if call.call_id == audit.call_id)
            if len(matching) != 1 or matching[0] != audit:
                raise AuditIntegrityError("cold paid phase changed its actual restored recorder identity")
            completion = retained.outcome.completion
            data = runtime.graph.batch
            if data is not None:
                if completion.tool_batch.calls != data.calls:
                    raise AuditIntegrityError("cold paid phase changed its actual restored issued calls")
                # Reuse the already restored SDK atoms; only owned carriers
                # are rebound. Warm phases never take this cold DATA path.
                completion = replace(completion, tool_batch=replace(completion.tool_batch, calls=data.calls))
            self.primary = RetainedPrimaryPhaseData(retained.response, _CallModelOutcome(completion, matching[0]))
        if self.primary.response.reference != compose[0].pending_response:
            raise AuditIntegrityError("live phase replaced its original paid response")
        return self.primary

    async def current(self, selected: SelectedContinuationState) -> tuple[RestoredContinuationRuntime, CurrentContinuationEnvInventory]:
        phase = await self._current_phase_read(selected)
        return phase.runtime, phase.inventory

    async def _current_phase_read(self, selected: SelectedContinuationState) -> _CurrentPhaseRead:
        running = self.work.running
        if type(running) is not ComposerOperationRunning or running.session_operation_context is not self.lease.context:
            raise AuditIntegrityError("live phase lost its actual running/physical owner")
        finish_read: ContinuationFinishRead | None = None
        try:
            inventory = await materialize_continuation_env_inventory(
                advisor=self.service._advisor_checkpoint, authority=self.authority, selected=selected, required_work=self.work
            )
        except ComposerOperationCancelledDuringTurn:
            names = await _read_current_env_names(self.service._advisor_checkpoint, selected.stack.logical.session_id)
            inventory = CurrentContinuationEnvInventory(
                running,
                ContinuationEnvSelection(selected.checkpoint_id, selected.stack.cursor, encode_continuation_snapshot(selected.stack)),
                names,
            )
            finish_read = ContinuationFinishRead(running, selected, inventory)

        def read_source() -> ContinuationRuntimeSource:
            nonlocal finish_read
            if finish_read is not None:
                return finish_read.read_runtime(self.authority)
            try:
                return self.authority.read_continuation_runtime(inventory.running, selected=selected, inventory=inventory)
            except ComposerOperationCancelledDuringTurn:
                finish_read = ContinuationFinishRead(running, selected, inventory)
                return finish_read.read_runtime(self.authority)

        source = await _registered_read(self.work, ContinuationRuntimeSource, read_source)
        if self.runtime is None:
            self.runtime = await restore_continuation_runtime(
                service=self.service,
                authority=self.authority,
                selected=selected,
                inventory=inventory,
                required_work=self.work,
                provider_owner=self.provider_owner,
                finish_read=finish_read,
            )
        elif self.runtime.source.selected != selected:
            runtime = self.runtime

            def refresh() -> RestoredContinuationRuntime:
                fresh = read_source()
                _require_runtime_source_refresh(source, fresh)
                runtime.materialization.advance_current(selected, inventory)
                following = replace(runtime, source=fresh)
                # Retain the actual fresh source before the async SQL bridge can
                # defer cancellation. No live messages/graph objects are rebuilt.
                self.runtime = following
                return following

            await _registered_read(self.work, RestoredContinuationRuntime, refresh)
        else:
            # Same SQL cut can arrive as a fresh selected object, including a
            # warm owner which has just observed durable Stop. Carry that
            # exact read choice without replacing live owners or their clocks.
            _require_runtime_source_refresh(self.runtime.source, source)
            self.runtime = replace(self.runtime, source=source)
        if self.runtime is None:
            raise AuditIntegrityError("live phase source lost its actual runtime")
        if self.runtime.source.selected is not selected:
            raise AuditIntegrityError("live phase read lost its actual fresh selection")
        if finish_read is not None:
            finish_read.require_selection(selected, inventory)
        return _CurrentPhaseRead(self.runtime, selected, inventory, finish_read)

    async def _retain(
        self, captured: CapturedContinuationRuntime, inventory: CurrentContinuationEnvInventory, transition: BatchTransition
    ) -> None:
        def retain() -> StoredContinuationSnapshot:
            stored = retain_batch_transition(
                self.authority, inventory.running, captured=captured, inventory=inventory, transition=transition
            )
            # Preserve the actual committing owner's return on its physical
            # thread before bridge delivery can surface a caller cancellation.
            self.latest_snapshot = stored
            return stored

        stored = await _registered(
            work=self.work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=retain,
            expected=StoredContinuationSnapshot,
        )
        selected = await load_selected_continuation(
            authority=self.authority, running=inventory.running, selected=stored, required_work=self.work
        )
        await self.current(selected)

    async def _capture_issued_finish(
        self, runtime: RestoredContinuationRuntime, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
    ) -> CapturedContinuationRuntime:
        def read_source() -> ContinuationRuntimeSource:
            try:
                return self.authority.read_continuation_runtime(inventory.running, selected=selected, inventory=inventory)
            except ComposerOperationCancelledDuringTurn:
                return self.authority.read_continuation_owed_tool_runtime(inventory.running, selected=selected, inventory=inventory)

        def capture() -> CapturedContinuationRuntime:
            source = read_source()
            captured = _capture_owners(runtime, source=source, inventory=inventory)
            following = read_source()
            if following.selected != source.selected or following.bootstrap != source.bootstrap or following.evidence != source.evidence:
                raise AuditIntegrityError("issued tool capture changed its complete current source during retention")
            return captured

        return await _registered_read(self.work, CapturedContinuationRuntime, capture)

    async def _initialize_batch(self, selected: SelectedContinuationState) -> None:
        runtime, inventory = await self.current(selected)
        result, live = await self._primary_for_phase(runtime, inventory, selected), runtime.live
        frame = selected.stack.frames[-1]
        if type(frame) is not ComposeFrame or frame.pending_response != result.response.reference:
            raise AuditIntegrityError("batch initialization changed its exact original paid response")
        self.service._enforce_tool_call_cap(
            assistant_tool_calls=result.outcome.completion.tool_batch.calls,
            state=live.state,
            initial_version=live.initial_version,
            composition_turns_used=live.composition_turns_used,
            discovery_turns_used=live.discovery_turns_used,
            recorder=live.recorder,
            failed_turn=live.failed_turn,
            persisted_tool_call_turn=live.persisted_tool_call_turn,
        )
        accumulator = BatchAccumulator(live.state, live.last_validation, live.last_runtime_preflight, live.advisor_calls_used)
        batch = ContinuationBatchState(
            live.state, live.last_validation, live.last_runtime_preflight, live.advisor_calls_used, entry_state=live.state
        )
        data = RestoredBatchData(batch, accumulator, result.outcome.completion.tool_batch.calls)
        context = await self._tool_context(runtime)
        await _initialize_continuation_batch_live(
            call_model=result.outcome, ctx=context, acc=accumulator, llm_messages=live.llm_messages, batch=batch
        )
        self.runtime = runtime = replace(runtime, graph=runtime.graph.with_batch(data))
        captured = await capture_continuation_runtime_owners(runtime, selected=selected, inventory=inventory)
        chunk = encode_private_chunk(
            codec="composer.coupled-batch-data.v1",
            canonical=encode_coupled_batch_data(batch=batch, accumulator=accumulator, calls=data.calls, env_ref_names=inventory.names),
            env_ref_names=inventory.names,
        )
        tool = ToolBatchFrame(
            phase="tool_ready", response=result.response.reference, accumulator=chunk.reference, next_tool_index=0, settled_effect_ids=()
        )
        root = captured.stack.model_copy(update={"frames": (*captured.stack.frames, tool)})
        following = CapturedContinuationRuntime(selected, root, _selected_chunks(root, captured.chunks, (chunk,)))
        await self._retain(following, inventory, "batch_initialized")

    async def _execute_one(self, selected: SelectedContinuationState) -> None:
        previous = self.business_stage
        if previous is not None and (previous.task is None or not previous.task.done() or not previous.observed):
            raise AuditIntegrityError("tool dispatch cannot replace its retained physical stage")
        self.business_stage = stage = self.lease.create_continuation_stage(
            self._execute_one_owned(selected), name="continuation-issued-tool"
        )
        await self.lease.join_continuation_stage(stage)

    async def _execute_one_owned(self, selected: SelectedContinuationState) -> None:
        runtime, inventory = await self.current(selected)
        data = runtime.graph.batch
        if data is None:
            raise AuditIntegrityError("live dispatch lost its actual batch/provider owners")
        result = await self._primary_for_phase(runtime, inventory, selected)
        batch, accumulator, live = data.batch, data.accumulator, runtime.live
        if batch.phase != "call_ready" or batch.current_call is not None or batch.next_tool_index >= len(data.calls):
            raise AuditIntegrityError("live dispatch lost its actual unissued next call")
        context = await self._tool_context(runtime)
        call = ContinuationCallState(batch.next_tool_index, data.calls[batch.next_tool_index], batch.state, batch.state.version)
        batch.current_call, batch.phase = call, "call_active"
        preparation = await _prepare_continuation_call_live(
            call_model=result.outcome, ctx=context, llm_messages=live.llm_messages, batch=batch, call=call
        )
        issued_effect_key: str | None = None
        if preparation == "prepared":
            if call.audit is None:
                raise AuditIntegrityError("tool preparation lost its actual dispatch audit")
            # Prepared DATA precedes the SQL issue owner. The canonical writer
            # binds this locator to its actual checkpoint/event/physical fence;
            # it must never be injected into the pure prepared call codec.
            issued_effect_key = f"tool:{result.response.reference.digest}:{call.index}"
            self.runtime = runtime = replace(
                runtime,
                graph=runtime.graph.with_selected_dispatch(
                    capture_dispatch_audit(call.audit, env_ref_names=inventory.names), context.tool_contract_dialect
                ),
            )
            captured = await capture_continuation_runtime_owners(runtime, selected=selected, inventory=inventory)
            top = captured.stack.frames[-1]
            if type(top) is not ToolBatchFrame:
                raise AuditIntegrityError("prepared tool capture changed its actual frame family")
            root = captured.stack.model_copy(
                update={
                    "frames": (*captured.stack.frames[:-1], top.model_copy(update={"phase": "effect_reconciling"})),
                    "effect_frontier": (
                        *captured.stack.effect_frontier,
                        EffectFrontierEntry(effect_id=issued_effect_key, disposition="dispatched", receipt_id=None),
                    ),
                }
            )
            await self._retain(CapturedContinuationRuntime(selected, root, captured.chunks), inventory, "tool_issued")
            self.failure_custody = ContinuationToolFailureCustody(
                inventory.running, self.work, result.response, batch, data.calls, live.recorder, call
            )
            await _execute_continuation_call_live(
                call_model=result.outcome,
                ctx=context,
                acc=accumulator,
                llm_messages=live.llm_messages,
                batch=batch,
                call=call,
                failure_custody=self.failure_custody,
            )
        if preparation == "stop":
            batch.current_call, batch.phase = None, "dispatch_ready"
            return
        batch.next_tool_index += 1
        batch.current_call = None
        batch.phase = "dispatch_ready" if batch.stop_issued_batch or batch.next_tool_index == len(data.calls) else "call_ready"
        live.state, live.last_validation, live.last_runtime_preflight, live.advisor_calls_used = (
            batch.state,
            batch.last_validation,
            batch.last_runtime_preflight,
            batch.advisor_calls_used,
        )
        if batch.service_failure is not None:
            custody = self.failure_custody
            if preparation != "prepared" or custody is None or issued_effect_key is None or self.runtime is None:
                raise AuditIntegrityError("service completion lost its actual issued native phase")
            if self.terminal_completion is not None:
                raise AuditIntegrityError("service completion cannot replace another native terminal owner")
            try:
                self.terminal_completion = TerminalServiceCompletion(custody, self.runtime, context, result.outcome, issued_effect_key)
                # This failed batch cannot be encoded or restored. Its last valid
                # issued SQL cut is only a source bookmark for terminal P4 proof.
                await self._persist_batch(self.runtime.source.selected)
            except BaseException as error:
                combined = custody.failure(error)
                if combined is error:
                    raise
                raise combined from combined.__cause__
            return
        if self.runtime is None:
            raise AuditIntegrityError("completed tool lost its retained runtime")
        runtime = self.runtime
        inventory = runtime.materialization.inventory
        selected = runtime.source.selected
        self.runtime = runtime = replace(runtime, graph=runtime.graph.without_selected_dispatch())
        captured = (
            await self._capture_issued_finish(runtime, selected=selected, inventory=inventory)
            if preparation == "prepared"
            else await capture_continuation_runtime_owners(runtime, selected=selected, inventory=inventory)
        )
        top = captured.stack.frames[-1]
        if type(top) is not ToolBatchFrame:
            raise AuditIntegrityError("completed tool capture changed its actual frame family")
        root = captured.stack.model_copy(update={"frames": (*captured.stack.frames[:-1], top.model_copy(update={"phase": "tool_ready"}))})
        if preparation == "prepared":
            if issued_effect_key is None:
                raise AuditIntegrityError("completed tool lost its actual issued effect locator")
            receipt = f"tool-result:{top.accumulator.digest}"
            root = root.model_copy(
                update={
                    "effect_frontier": tuple(
                        EffectFrontierEntry(effect_id=entry.effect_id, disposition="committed", receipt_id=receipt)
                        if entry.effect_id == issued_effect_key
                        else entry
                        for entry in root.effect_frontier
                    ),
                    "frames": (
                        *root.frames[:-1],
                        root.frames[-1].model_copy(update={"settled_effect_ids": (*top.settled_effect_ids, issued_effect_key)}),
                    ),
                }
            )
        await self._retain(
            CapturedContinuationRuntime(selected, root, captured.chunks),
            inventory,
            "tool_completed" if preparation == "prepared" else "tool_preparation_completed",
        )

    async def _persist_batch(self, selected: SelectedContinuationState) -> None:
        terminal = self.terminal_completion
        if terminal is None:
            phase_read = await self._current_phase_read(selected)
            runtime, inventory = phase_read.runtime, phase_read.inventory
            result = await self._primary_for_phase(runtime, inventory, selected)
            context = await build_runtime_tool_batch_context(
                runtime, cancellation_requested=self.cancellation_requested, phase_read=phase_read
            )
            call_model = result.outcome
        else:
            terminal.require_native()
            if self.runtime is not terminal.runtime or selected is not terminal.runtime.source.selected or terminal.p4 is not None:
                raise terminal.custody.failure(AuditIntegrityError("terminal P4 cannot replace or replay its actual native completion"))
            runtime = terminal.runtime
            inventory = runtime.materialization.inventory
            context, call_model = terminal.context, terminal.call_model
        data = runtime.graph.batch
        if data is None or data.batch.phase != "dispatch_ready":
            raise AuditIntegrityError("live P4 lost its actual completed batch/provider owners")
        dispatch, advisor_calls = _finish_continuation_batch_live(
            call_model=call_model,
            ctx=context,
            acc=data.accumulator,
            batch=data.batch,
            failure_custody=self.failure_custody,
        )
        live, bindings = runtime.live, runtime.bindings
        self.p4 = owner = ContinuationLiveP4(
            self.authority,
            selected,
            inventory,
            self.work,
            data,
            runtime,
            withheld_replies=live.recorder.withheld_replies[runtime.drained.withheld_replies :],
        )
        if terminal is not None:
            terminal.bind_p4(owner)

        async def publish() -> None:
            deadline_owner = self.work.coordinator.sql_deadline_owner
            if deadline_owner is not None:
                if deadline_owner.running is not inventory.running:
                    raise AuditIntegrityError("P4 finishing changed its actual original timing owner")
                # This retained child only finishes the actual existing batch;
                # its SQL owner still proves all current/pre-Stop authority.
                # Spend the original action's reserve without restarting D.
                deadline_owner.begin_settlement()
            original: BaseException | None = None
            try:
                await self.service._persist_turn_audit(
                    tool_outcomes=dispatch.tool_outcomes,
                    mutation_ceiling=bindings.mutation_ceiling,
                    authority_revocation=dispatch.authority_revocation,
                    base_state=data.batch.entry_state,
                    decoded_args_by_call_id=dispatch.decoded_args_by_call_id,
                    private_authorings=dispatch.private_authorings,
                    private_invocations=dispatch.private_invocations,
                    assistant_message=dispatch.assistant_message,
                    raw_assistant_content=dispatch.raw_assistant_content,
                    assistant_tool_calls=dispatch.assistant_tool_calls,
                    crash_pending=(
                        dispatch.plugin_crash is not None
                        or dispatch.advisor_failure is not None
                        or dispatch.service_failure is not None
                        or (
                            data.batch.retained_failure is not None
                            and data.batch.retained_failure.kind in ("plugin_crash", "advisor_failure")
                        )
                    ),
                    session_id=bindings.session_id,
                    session_operation_context=bindings.session_operation_context,
                    current_state_id=live.current_state_id,
                    persisted_tool_call_turn=live.persisted_tool_call_turn,
                    persisted_assistant_message_id=live.persisted_assistant_message_id,
                    persisted_assistant_content=live.persisted_assistant_content,
                    ingress=bindings.ingress,
                    chat_ingress_inputs=None if bindings.chat_ingress_inputs is None else list(bindings.chat_ingress_inputs),
                    advisor_repair_context_introduced=live.advisor_repair_context_introduced,
                    required_work=self.work,
                    turn_evidence=live.turn_evidence,
                    continuation=owner,
                    terminal_completion=terminal,
                )
            except BaseException as failure:
                original = failure if self.failure_custody is None else self.failure_custody.failure(failure)
            finally:
                # turn_audit retains actual committed publication before a
                # deferred cancellation or verification failure may escape.
                persist = owner.publication
                if persist is not None:
                    live.current_state_id = persist.current_state_id
                    live.persisted_assistant_message_id = persist.persisted_assistant_message_id
                    live.persisted_assistant_content = persist.persisted_assistant_content
                    live.persisted_tool_call_turn, live.failed_turn = persist.persisted_tool_call_turn, persist.failed_turn
                    live.advisor_calls_used = advisor_calls
            if owner.publication is not None:
                # The one checkpoint projection reconciled both actual SQL
                # outputs before completing. Never submit a second apply here.
                self.runtime = owner.runtime
                if original is not None:
                    raise original
            elif original is not None:
                raise original
            try:
                owner.raise_after_publication()
            except BaseException as failure:
                if self.failure_custody is not None:
                    combined = self.failure_custody.failure(failure)
                    if combined is failure:
                        raise
                    raise combined from combined.__cause__
                raise
            custody = self.failure_custody
            if custody is not None and custody.completed:
                if custody.wrapper is not None:
                    raise custody.wrapper from custody.original
                if custody.original is not None:
                    raise custody.original

        critical = self.lease.create_continuation_stage(publish(), name="continuation-required-p4")
        await self.lease.join_continuation_stage(critical)

    async def finish_owed_prefix(self, selected: SelectedContinuationState) -> None:
        """Finish an actual retained prefix; the SQL owner proves durable Stop."""
        if self.terminal_completion is not None:
            await self._persist_batch(selected)
            return
        top = selected.stack.frames[-1]
        if type(top) is not ToolBatchFrame or top.phase != "tool_ready" or top.accumulator.codec != "composer.coupled-batch-data.v1":
            return
        runtime, _inventory = await self.current(selected)
        data = runtime.graph.batch
        if data is None or not data.batch.tool_outcomes:
            return
        if data.batch.phase == "call_ready":
            data.batch.stop_issued_batch = True
            data.batch.phase = "dispatch_ready"
        if data.batch.phase == "dispatch_ready":
            await self._persist_batch(selected)

    async def advance(self, selected: SelectedContinuationState) -> None:
        if self.terminal_completion is not None:
            await self._persist_batch(selected)
            return
        top = selected.stack.frames[-1]
        if type(top) is ComposeFrame and top.phase == "response_retained":
            await self._initialize_batch(selected)
            return
        if type(top) is ToolBatchFrame and top.phase == "tool_ready":
            runtime, _inventory = await self.current(selected)
            if runtime.graph.batch is not None and runtime.graph.batch.batch.phase == "call_ready":
                await self._execute_one(selected)
                return
            if runtime.graph.batch is not None and runtime.graph.batch.batch.phase == "dispatch_ready":
                await self._persist_batch(selected)
                return
        raise AuditIntegrityError("selected phase has no matching live tool action")''',
    "p4.ContinuationLiveP4": r'''@dataclass(slots=True)
class ContinuationLiveP4:
    authority: ComposerAsyncOperationAuthority
    selected: SelectedContinuationState
    inventory: CurrentContinuationEnvInventory
    work: RequiredWorkBinding
    data: RestoredBatchData
    runtime: RestoredContinuationRuntime
    withheld_replies: tuple[WithheldReply, ...] = ()
    commit: ContinuationOwnerCommit | None = field(default=None, init=False)
    prepared: PreparedP4Value | None = field(default=None, init=False)
    preparation: RequiredSQLFinishOnce[ContinuationOwnerCommit] | None = field(default=None, init=False)
    physical: RequiredSQLFinishOnce[CommittedContinuationP4] | None = field(default=None, init=False)
    committed_outcome: CommittedContinuationP4 | None = field(default=None, init=False)
    publication: _PersistOutcome | None = field(default=None, init=False)
    captured: CapturedContinuationRuntime | None = field(default=None, init=False)
    application_sql: RequiredSQLFinishOnce[StoredP4Application] | None = field(default=None, init=False)
    application: StoredP4Application | StoredTerminalP4Application | None = field(default=None, init=False)
    writer_observation: P4WriterObservation | None = field(default=None, init=False)
    terminal_completion: TerminalServiceCompletion | None = field(default=None, init=False)
    _checkpoint_ticket: RequiredWorkTicket | None = field(default=None, init=False, repr=False)
    deferred_failures: list[BaseException] = field(default_factory=list, init=False)
    deferred_cancellations: list[asyncio.CancelledError] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if (
            type(self.authority) is not ComposerAsyncOperationAuthority
            or type(self.selected) is not SelectedContinuationState
            or type(self.inventory) is not CurrentContinuationEnvInventory
            or type(self.work) is not RequiredWorkBinding
            or type(self.data) is not RestoredBatchData
            or type(self.runtime) is not RestoredContinuationRuntime
            or self.runtime.graph.batch is not self.data
            or self.runtime.bindings.required_work is not self.work
            or self.work.running is None
            or self.inventory.running is not self.work.running
        ):
            raise AuditIntegrityError("live P4 lost its actual current phase owners")
        self.work.validate_context(self.inventory.running.session_operation_context)

    def _retain_cancellations[T](self, outcome: RequiredSQLFinishOnce[T]) -> None:
        for cancellation in outcome.deferred_cancellations:
            if all(cancellation is not previous for previous in self.deferred_cancellations):
                self.deferred_cancellations.append(cancellation)

    async def prepare(self, prepared: PreparedP4Value) -> ContinuationOwnerCommit:
        if self.preparation is not None or self.prepared is not None:
            raise AuditIntegrityError("live P4 preparation cannot replace a retained physical outcome")
        names = await _read_current_env_names(
            self.runtime.materialization.service._advisor_checkpoint, self.selected.stack.logical.session_id
        )
        self.inventory = CurrentContinuationEnvInventory(self.inventory.running, self.inventory.selection, names)
        pending = replace(
            self.data.batch, phase="p4_pending", p4_input_json=encode_prepared_p4(prepared, env_ref_names=self.inventory.names)
        )
        chunk = encode_p4_pending_batch_chunk(
            batch=pending, accumulator=self.data.accumulator, calls=self.data.calls, env_ref_names=self.inventory.names
        )
        members = prepared_p4_members(prepared, env_ref_names=self.inventory.names)
        recorded = self.runtime.live.recorder.invocations
        linked = self.runtime.live.recorder.visible_tool_events
        for expected, row in zip(members.invocations, members.redacted_rows, strict=True):
            actual = tuple(invocation for invocation in recorded if invocation.tool_call_id == expected.tool_call_id)
            if len(actual) != 1 or actual[0] != expected:
                raise AuditIntegrityError("P4 redaction lost its actual recorded invocation")
            if row.visible_event is not None:
                previous = tuple(event for invocation, event in linked if invocation is actual[0])
                if previous and previous != (row.visible_event,):
                    raise AuditIntegrityError("P4 redaction replaced its actual visible event")
                if not previous:
                    self.runtime.live.recorder.record_visible_tool_event(actual[0], row.visible_event)
        pending_data = RestoredBatchData(pending, self.data.accumulator, self.data.calls)
        runtime = replace(self.runtime, graph=self.runtime.graph.with_batch(pending_data))

        def read_source() -> ContinuationRuntimeSource:
            try:
                return self.authority.read_continuation_runtime(self.inventory.running, selected=self.selected, inventory=self.inventory)
            except ComposerOperationCancelledDuringTurn:
                # A real durable Stop selects the separate read facet. It must
                # prove immutable pre-Stop issue ordering again; no caller flag
                # or old frontier grants the pending P4 write.
                return self.authority.read_continuation_owed_batch_runtime(
                    self.inventory.running, selected=self.selected, inventory=self.inventory
                )

        def capture() -> CapturedContinuationRuntime:
            source = read_source()
            captured = _capture_owners(runtime, source=source, inventory=self.inventory)
            actual = read_source()
            if actual.selected != source.selected or actual.bootstrap != source.bootstrap or actual.evidence != source.evidence:
                raise AuditIntegrityError("P4 capture changed its complete current source during retention")
            self.captured = captured
            return captured

        captured = await _registered_read(self.work, CapturedContinuationRuntime, capture)
        top = captured.stack.frames[-1]
        if type(top) is not ToolBatchFrame:
            raise AuditIntegrityError("P4 capture lost its actual completed batch frame")
        sql, projection = self.work.reserve_pair(
            RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION
        )
        try:
            outcome = await run_required_sql_finish_once(
                sql,
                self.authority.prepare_continuation_p4,
                self.inventory.running,
                expected_checkpoint_id=self.selected.checkpoint_id,
                expected_cursor=self.selected.stack.cursor,
                pending_batch=chunk,
                env_ref_names=self.inventory.names,
                captured=captured,
            )
        except BaseException:
            projection.complete_without_submission()
            raise
        self.preparation = outcome
        self._retain_cancellations(outcome)
        try:
            self.work.coordinator.validate_continuation_sql_outcome(
                ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_STATE_SQL, actual_outcome=outcome
            )
        except BaseException:
            projection.complete_without_submission()
            raise
        if type(outcome) is RequiredSQLRaised:
            projection.complete_without_submission()
            self.raise_after_publication(outcome.error)
        if type(outcome) is not RequiredSQLReturned:
            projection.complete_without_submission()
            raise AuditIntegrityError("live P4 preparation returned another physical outcome")
        projection.begin_projection()
        try:
            if type(outcome.value) is not ContinuationOwnerCommit or outcome.value.running is not self.inventory.running:
                raise AuditIntegrityError("live P4 preparation changed its actual current owner")
            self.commit, self.prepared = outcome.value, prepared
            self.writer_observation = P4WriterObservation(outcome.value, prepared)
            self.data.batch.phase, self.data.batch.p4_input_json = pending.phase, pending.p4_input_json
        except BaseException as error:
            projection.complete_owned(error)
            raise
        projection.complete_owned()
        return outcome.value

    async def prepare_terminal(self, prepared: PreparedP4Value, completion: TerminalServiceCompletion) -> ContinuationOwnerCommit:
        from elspeth.web.composer.continuation_tool_failure import TerminalServiceCompletion

        if type(completion) is not TerminalServiceCompletion or self.preparation is not None or self.prepared is not None:
            raise AuditIntegrityError("terminal P4 cannot replace its native completion or preparation")
        completion.require_p4(self)
        names = await _read_current_env_names(
            self.runtime.materialization.service._advisor_checkpoint, self.selected.stack.logical.session_id
        )
        self.inventory = CurrentContinuationEnvInventory(self.inventory.running, self.inventory.selection, names)
        members = prepared_p4_members(prepared, env_ref_names=names)
        recorded = self.runtime.live.recorder.invocations
        linked = self.runtime.live.recorder.visible_tool_events
        for expected, row in zip(members.invocations, members.redacted_rows, strict=True):
            actual = tuple(invocation for invocation in recorded if invocation.tool_call_id == expected.tool_call_id)
            if len(actual) != 1 or actual[0] != expected:
                raise AuditIntegrityError("terminal P4 redaction changed its actual recorded invocation")
            if row.visible_event is not None:
                previous = tuple(event for invocation, event in linked if invocation is actual[0])
                if previous and previous != (row.visible_event,):
                    raise AuditIntegrityError("terminal P4 redaction changed its actual visible event")
                if not previous:
                    self.runtime.live.recorder.record_visible_tool_event(actual[0], row.visible_event)
        sql, projection = self.work.reserve_pair(
            RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION
        )
        try:
            outcome = await run_required_sql_finish_once(
                sql,
                self.authority.prepare_terminal_continuation_p4,
                self.inventory.running,
                completion=completion,
                prepared=prepared,
                env_ref_names=names,
            )
        except BaseException:
            projection.complete_without_submission()
            raise
        self.preparation = outcome
        self._retain_cancellations(outcome)
        try:
            self.work.coordinator.validate_continuation_sql_outcome(
                ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_STATE_SQL, actual_outcome=outcome
            )
        except BaseException:
            projection.complete_without_submission()
            raise
        if type(outcome) is RequiredSQLRaised:
            projection.complete_without_submission()
            self.raise_after_publication(outcome.error)
        if type(outcome) is not RequiredSQLReturned:
            projection.complete_without_submission()
            raise AuditIntegrityError("terminal P4 preparation returned another physical outcome")
        projection.begin_projection()
        try:
            completion.require_p4(self)
            if type(outcome.value) is not ContinuationOwnerCommit or outcome.value.running is not self.inventory.running:
                raise AuditIntegrityError("terminal P4 preparation changed its actual current owner")
            self.commit, self.prepared = outcome.value, prepared
            self.writer_observation = P4WriterObservation(outcome.value, prepared)
            self.terminal_completion = completion
        except BaseException as error:
            projection.complete_owned(error)
            raise
        projection.complete_owned()
        return outcome.value

    def bind_checkpoint_ticket(self, ticket: RequiredWorkTicket) -> None:
        """Capture this P4's actual reservation before its physical invocation.

        This pointer grants no write. The service and transaction still prove
        source/current authority, and acceptance separately proves its Future.
        """
        coordinator = self.work.coordinator
        with coordinator._lock:
            if self._checkpoint_ticket is not None or self.physical is not None:
                raise AuditIntegrityError("live P4 checkpoint ticket is already bound or observed")
            coordinator._registered(ticket, RequiredWorkSource.COMPOSE_CHECKPOINT_SQL)
            running = self.inventory.running
            self.work.validate_context(running.session_operation_context)
            if (
                self.work.running is not running
                or self.commit is None
                or self.commit.running is not running
                or self.prepared is None
                or self.writer_observation is None
                or self.writer_observation.commit is not self.commit
                or self.writer_observation.prepared is not self.prepared
                or ticket.key.authority.context != running.session_operation_context
                or ticket.key.authority.durable_operation_id != running.claim.operation_id
                or ticket.key.authority.claim_attempt != running.claim.attempt
                or ticket.key.transition_ordinal != self.work.transition_ordinal
                or ticket.key.semantic_ordinal != self.work.semantic_ordinal
                or ticket.key.subphase is not RequiredWorkSubphase.SQL_INITIAL
                or ticket.key.sql_attempt_ordinal != 0
            ):
                raise AuditIntegrityError("live P4 checkpoint ticket crossed its actual prepared scope")
            with ticket._lock:
                if (
                    ticket._complete
                    or ticket._future is not None
                    or ticket._submission_unknown
                    or ticket._projection_started
                    or ticket._finish_once_handoff is not None
                ):
                    raise AuditIntegrityError("live P4 checkpoint binding must precede physical submission")
                self._checkpoint_ticket = ticket

    def accept_physical(self, ticket: RequiredWorkTicket, outcome: RequiredSQLFinishOnce[CommittedContinuationP4]) -> AuditOutcome:
        with self.work.coordinator._lock:
            if self.physical is not None:
                raise AuditIntegrityError("live P4 cannot replace its retained physical SQL result")
            self.physical = outcome
            self._retain_cancellations(outcome)
            if self.writer_observation is not None:
                self.deferred_failures.extend(self.writer_observation.prior_errors)
            if self.writer_observation is not None and self.writer_observation.original_error is not None:
                self.deferred_failures.append(self.writer_observation.original_error)
            if self._checkpoint_ticket is None or ticket is not self._checkpoint_ticket:
                raise AuditIntegrityError("live P4 replaced its actual bound checkpoint ticket")
            self.work.coordinator.validate_compose_checkpoint_sql_outcome(ticket=ticket, actual_outcome=outcome)
        if type(outcome) is RequiredSQLRaised:
            self.raise_after_publication(outcome.error)
        if type(outcome) is not RequiredSQLReturned or type(outcome.value) is not CommittedContinuationP4:
            raise AuditIntegrityError("live P4 returned a foreign physical publication")
        observed = self.writer_observation
        if (
            observed is None
            or not observed.committed
            or outcome.value.audit_outcome is not observed.candidate
            or outcome.value.application is not observed.application_candidate
        ):
            raise AuditIntegrityError("live P4 replaced its actual committing owner's outputs")
        self.committed_outcome = outcome.value
        return outcome.value.audit_outcome

    def raise_after_publication(self, original: BaseException | None = None) -> None:
        errors: list[BaseException] = [] if original is None else [original]
        errors.extend(failure for failure in self.deferred_failures if failure is not original)
        errors.extend(cancellation for cancellation in self.deferred_cancellations if cancellation is not original)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("Live P4 retained outcomes", errors)''',
    "service.ComposerServiceImpl._persist_turn_audit": r'''async def _persist_turn_audit(
    self,
    *,
    tool_outcomes: tuple[_ToolOutcome, ...],
    mutation_ceiling: ComposerMutationCeiling | None,
    authority_revocation: ComposerMutationRevocation | None,
    base_state: CompositionState,
    decoded_args_by_call_id: Mapping[str, Mapping[str, Any]],
    private_authorings: tuple[PrivateToolAuthoring, ...],
    private_invocations: tuple[ComposerToolInvocation, ...],
    assistant_message: _AdmittedAssistantMessage,
    raw_assistant_content: str | None,
    assistant_tool_calls: tuple[_AdmittedToolCall, ...],
    crash_pending: bool,
    session_id: str | None,
    session_operation_context: SessionOperationContext | None = None,
    current_state_id: str | None,
    persisted_tool_call_turn: bool,
    persisted_assistant_message_id: str | None,
    # REQUIRED (no default): the content of the row named by
    # ``persisted_assistant_message_id``. Threading the id without it is the
    # shape that silently regresses to re-emitting already-persisted prose
    # (elspeth-d581b3da7f), so a missed site must fail loudly here rather
    # than default to None.
    persisted_assistant_content: str | None,
    ingress: CompositionIngressRecord | None = None,
    chat_ingress_inputs: list[ChatIngressInput] | None = None,
    advisor_repair_context_introduced: bool = False,
    required_work: RequiredWorkBinding | None = None,
    turn_evidence: TurnEvidence | None = None,
    continuation: ContinuationLiveP4 | None = None,
    terminal_completion: TerminalServiceCompletion | None = None,
) -> _PersistOutcome:
    """Phase P4 of the compose loop — delegates to :func:`turn_audit.persist_turn_audit`."""
    from elspeth.web.composer.turn_audit import persist_turn_audit

    persisted_assistant_message = assistant_message
    persisted_raw_assistant_content = raw_assistant_content
    if advisor_repair_context_introduced:
        # The assistant row below holds the fixed status line, so the
        # turn's own prose is kept as a non-rendered audit row first.
        await self._completion._persist_withheld_reply(
            "advisor_repair_tool_turn",
            assistant_message.content or "",
            session_id=session_id,
            session_operation_context=session_operation_context,
        )
        persisted_assistant_message = _AdmittedAssistantMessage(
            content=_ADVISOR_REPAIR_INTERMEDIATE_PUBLIC_MESSAGE,
        )
        persisted_raw_assistant_content = None

    return await persist_turn_audit(
        sessions_service=self._sessions_service,
        redaction_telemetry=self._redaction_telemetry,
        tool_outcomes=tool_outcomes,
        mutation_ceiling=mutation_ceiling,
        authority_revocation=authority_revocation,
        base_state=base_state,
        decoded_args_by_call_id=decoded_args_by_call_id,
        private_authorings=private_authorings,
        private_invocations=private_invocations,
        assistant_message=persisted_assistant_message,
        raw_assistant_content=persisted_raw_assistant_content,
        assistant_tool_calls=assistant_tool_calls,
        crash_pending=crash_pending,
        session_id=session_id,
        session_operation_context=session_operation_context,
        current_state_id=current_state_id,
        persisted_tool_call_turn=persisted_tool_call_turn,
        persisted_assistant_message_id=persisted_assistant_message_id,
        persisted_assistant_content=persisted_assistant_content,
        ingress=ingress,
        chat_ingress_inputs=chat_ingress_inputs,
        assistant_row_uses_current_dispatch=not advisor_repair_context_introduced,
        required_work=required_work,
        turn_evidence=turn_evidence,
        continuation=continuation,
        terminal_completion=terminal_completion,
    )''',
    "turn_audit.persist_turn_audit": r'''async def persist_turn_audit(
    *,
    sessions_service: SessionServiceProtocol | None,
    redaction_telemetry: RedactionTelemetry,
    tool_outcomes: tuple[_ToolOutcome, ...],
    mutation_ceiling: ComposerMutationCeiling | None,
    authority_revocation: ComposerMutationRevocation | None,
    base_state: CompositionState,
    decoded_args_by_call_id: Mapping[str, Mapping[str, Any]],
    private_authorings: tuple[PrivateToolAuthoring, ...],
    private_invocations: tuple[ComposerToolInvocation, ...],
    assistant_message: _AdmittedAssistantMessage,
    raw_assistant_content: str | None,
    assistant_tool_calls: tuple[_AdmittedToolCall, ...],
    crash_pending: bool,
    session_id: str | None,
    session_operation_context: SessionOperationContext | None,
    current_state_id: str | None,
    persisted_tool_call_turn: bool,
    persisted_assistant_message_id: str | None,
    persisted_assistant_content: str | None,
    assistant_row_uses_current_dispatch: bool,
    ingress: CompositionIngressRecord | None = None,
    chat_ingress_inputs: list[ChatIngressInput] | None = None,
    required_work: RequiredWorkBinding | None = None,
    turn_evidence: TurnEvidence | None = None,
    continuation: ContinuationLiveP4 | None = None,
    terminal_completion: TerminalServiceCompletion | None = None,
) -> _PersistOutcome:
    """Phase P4 of the compose loop — redact then persist the turn audit.

    Two sub-steps that share an invariant (a mid-step raise must leave
    the DB in its pre-step shape):

    1. **Redaction (pure / async).** Walks ``tool_outcomes`` via the
       redaction manifest and builds the immutable
       ``redacted_assistant_tool_calls`` / ``redacted_tool_rows`` shapes
       the persister expects.
    2. **Persistence.** Calls
       ``sessions_service.persist_compose_turn_async`` exactly once
       when ``session_id`` is set. The AuditIntegrityError catch
       stamps ``failed_turn`` with ``tool_responses_persisted=0`` and
       re-raises so the route handler sees the partial-write story.
       Unwind-audit invariants are checked after the persist returns;
       failures raise additional AuditIntegrityError(s).

    Crash propagation is intentionally *not* in this helper. P3 supplies a
    discriminated carrier (plugin crash or first-party advisor failure), and
    the driver raises it only after this phase publishes the closed row.

    ``assistant_row_uses_current_dispatch`` is the caller-owned P4
    disposition for whether ``assistant_message`` still contains the current
    dispatch's model prose. It is required because the caller performs the
    advisor-repair substitution; row presence cannot recover that fact.
    """
    from pydantic import ValidationError as PydanticValidationError

    from elspeth.contracts.freeze import deep_thaw
    from elspeth.web.composer.pipeline_custody import (
        inline_custody_audit_projection,
        inline_custody_manifest_redaction_input,
    )
    from elspeth.web.composer.redaction import (
        MANIFEST,
        redact_arg_error_response,
        redact_failure_response,
        redact_tool_call_arguments,
    )

    if terminal_completion is not None and continuation is None:
        raise AuditIntegrityError("terminal service publication cannot use a non-continuation writer")

    redacted_assistant_tool_calls: tuple[Mapping[str, Any], ...] = ()
    for index, tool_outcome in enumerate(tool_outcomes):
        tc = tool_outcome.call
        decoded_args: dict[str, Any]
        persisted_arguments: Mapping[str, Any]
        if tc.id in decoded_args_by_call_id:
            # deep_thaw restores plain dict/list types from any
            # MappingProxyType / tuple introduced by the carrier's
            # freeze_fields contract. redact_tool_call_arguments has a
            # ``dict[str, Any]`` signature and json.dumps cannot serialise
            # MappingProxyType, so the thaw here is load-bearing once
            # _DispatchOutcome carries frozen args.
            decoded_args = deep_thaw(decoded_args_by_call_id[tc.id])
        elif tool_outcome.error_class is not None:
            try:
                decoded_json = bounded_json_loads(
                    tc.function.arguments,
                    label="composer tool-call arguments",
                )
            except (TypeError, ValueError):
                decoded_args = {
                    "_redaction_status": INVALID_TOOL_ARGUMENTS_REDACTION_STATUS,
                    "error_class": tool_outcome.error_class,
                }
            else:
                decoded_args = (
                    # bounded_json_loads forwards to json.loads with no
                    # object_hook and no object_pairs_hook at this call site,
                    # so a decoded object is always an exact dict, never a
                    # subclass.
                    {"_decoded_non_object": decoded_json}
                    if type(decoded_json) is not dict
                    else {
                        "_redaction_status": INVALID_TOOL_ARGUMENTS_REDACTION_STATUS,
                        "error_class": tool_outcome.error_class,
                    }
                )
        else:
            decoded_args = {"_raw_arguments": tc.function.arguments}
        is_arg_error = tool_outcome.error_class is not None and not (crash_pending and index == len(tool_outcomes) - 1)
        if is_arg_error:
            arg_error_projection = redact_arg_error_response(
                error_class=tool_outcome.error_class,
                error_category=tool_outcome.error_category,
                error_message=None,
            )
            persisted_arguments = {
                "_redaction_status": INVALID_TOOL_ARGUMENTS_REDACTION_STATUS,
                "error_class": arg_error_projection["error_class"],
                "field_count": len(decoded_args),
            }
        elif tc.function.name in MANIFEST:
            try:
                redaction_input = (
                    inline_custody_manifest_redaction_input(decoded_args) if tc.function.name == "set_pipeline" else decoded_args
                )
                persisted_arguments = redact_tool_call_arguments(
                    tc.function.name,
                    redaction_input,
                    telemetry=redaction_telemetry,
                )
                if tc.function.name == "set_pipeline":
                    persisted_arguments = cast(
                        dict[str, Any],
                        inline_custody_audit_projection(persisted_arguments),
                    )
            except PydanticValidationError:
                if tool_outcome.error_class is None:
                    raise
                failure_projection = redact_failure_response(
                    status=ComposerToolStatus.PLUGIN_CRASH.value,
                    error_class=tool_outcome.error_class,
                    error_message=tool_outcome.error_message,
                )
                persisted_arguments = {
                    "_redaction_status": INVALID_TOOL_ARGUMENTS_REDACTION_STATUS,
                    "error_class": failure_projection["error_class"],
                    "field_count": len(decoded_args),
                }
        else:
            persisted_arguments = unknown_tool_arguments_redaction(telemetry=redaction_telemetry)
        redacted_assistant_tool_calls = (
            *redacted_assistant_tool_calls,
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": json.dumps(persisted_arguments),
                },
                # The call's wire facts (D1): which strict key was sent for the
                # tool and whether its raw arguments conformed to the W they
                # were sent under. Always present; null when not applicable.
                "strict_sent": tool_outcome.strict_sent,
                "wire_conformant": tool_outcome.wire_conformant,
            },
        )

    def visible_event_for(outcome: _ToolOutcome, display_result: str) -> VisibleToolEvent | None:
        if session_operation_context is None:
            return None
        authoring = next((item for item in private_authorings if item.tool_call_id == outcome.call.id), None)
        received, effective, selected = (
            turn_evidence.transcript.current_tool_strings(outcome.call.id, outcome.call.function.name)
            if turn_evidence is not None
            else (None, None, None)
        )
        display_call = next(item for item in redacted_assistant_tool_calls if item["id"] == outcome.call.id)
        return VisibleToolEvent.capture(
            context=session_operation_context,
            request_id=turn_evidence.request_id if turn_evidence is not None else None,
            tool_call_id=outcome.call.id,
            tool_name=outcome.call.function.name,
            received_arguments=received if received is not None else authoring.raw_arguments if authoring is not None else None,
            effective_arguments=effective if effective is not None else outcome.call.function.arguments,
            selected_result=selected,
            display_arguments=display_call["function"]["arguments"],
            display_result=display_result,
        )

    redacted_rows: list[RedactedToolRow] = []
    for index, tool_outcome in enumerate(tool_outcomes):
        display_result = _serialize_response_via_walker(
            tool_outcome,
            telemetry=redaction_telemetry,
            failure_status=(
                None
                if tool_outcome.error_class is None
                else (
                    ComposerToolStatus.PLUGIN_CRASH if crash_pending and index == len(tool_outcomes) - 1 else ComposerToolStatus.ARG_ERROR
                )
            ),
        )
        redacted_rows.append(
            RedactedToolRow(
                tool_call_id=tool_outcome.call.id,
                content=display_result,
                composition_state_payload=(
                    _state_payload_for_compose_turn(tool_outcome.response, ingress=ingress, chat_ingress_inputs=chat_ingress_inputs)
                    if tool_outcome.post_version > tool_outcome.pre_version
                    else None
                ),
                visible_event=visible_event_for(tool_outcome, display_result),
            )
        )
    redacted_tool_rows = tuple(redacted_rows)
    revoked_tool_rows = (
        _revoked_graph_rows(
            rows=redacted_tool_rows,
            outcomes=tool_outcomes,
            base_state=base_state,
            redaction_telemetry=redaction_telemetry,
        )
        if session_id is not None
        else redacted_tool_rows
    )
    failed_turn: FailedTurnMetadata | None = None
    unwind_audit_failed = False
    checkpoint_projection = None
    effect_verification_failure = None
    audit_outcome: AuditOutcome | None = None
    effects_before = turn_evidence.effects if turn_evidence is not None else None
    if session_id is not None:
        if type(session_operation_context) is not SessionOperationContext:
            raise AuditIntegrityError("Compose turn audit persistence requires exact session operation authority")
        if sessions_service is None:
            raise RuntimeError("sessions_service not wired")
        from elspeth.web.composer.audit_storage import _validated_canonical_mapping_pair, private_failure_evidence
        from elspeth.web.composer.diagnostic_evidence import build_private_tool_evidence, diagnostic_evidence_identity

        authorings = {authoring.tool_call_id: authoring for authoring in private_authorings}
        invocations = {invocation.tool_call_id: invocation for invocation in private_invocations}
        expected_ids = {outcome.call.id for outcome in tool_outcomes}
        if (
            set(authorings) != expected_ids
            or len(authorings) != len(private_authorings)
            or set(invocations) != expected_ids
            or len(invocations) != len(private_invocations)
        ):
            raise AuditIntegrityError("private tool checkpoint lost its exact authoring or invocation set")
        private_tool_evidence = []
        for outcome in tool_outcomes:
            authoring = authorings[outcome.call.id]
            invocation = invocations[outcome.call.id]
            if invocation.tool_name != outcome.call.function.name:
                raise AuditIntegrityError("private tool checkpoint invocation names another tool")
            if invocation.private_authoring is not None and invocation.private_authoring != authoring:
                raise AuditIntegrityError("private tool checkpoint receipt differs from its invocation")
            if invocation.result_canonical is not None:
                if invocation.result_hash is None:
                    raise AuditIntegrityError("private tool checkpoint result has no binding")
                _validated_canonical_mapping_pair(invocation.result_canonical, invocation.result_hash, label="private checkpoint result")
            elif invocation.result_hash is not None:
                raise AuditIntegrityError("private tool checkpoint absent result has a hash")
            private_tool_evidence.append(
                build_private_tool_evidence(
                    session_id=session_id,
                    operation_id=session_operation_context.fence.operation_id,
                    operation_epoch=session_operation_context.fence.operation_epoch,
                    tool_call_id=outcome.call.id,
                    tool_name=outcome.call.function.name,
                    status=invocation.status.value,
                    arguments_canonical=authoring.arguments_canonical,
                    authority_arguments_canonical=authoring.authority_arguments_canonical,
                    raw_arguments=authoring.raw_arguments,
                    result_canonical=invocation.result_canonical,
                    error_category=outcome.error_category.value if outcome.error_category is not None else None,
                )
            )
        private_tool_failures = tuple(
            private_failure_evidence(
                invocation.private_failure,
                identity=diagnostic_evidence_identity("tool-failure", invocation.tool_call_id),
                tool_call_id=invocation.tool_call_id,
                status="tool_failure",
                context=session_operation_context,
            )
            for invocation in private_invocations
            if invocation.private_failure is not None
        )
        checkpoint_sql = checkpoint_projection = None
        if required_work is not None:
            if type(required_work) is not RequiredWorkBinding:
                raise AuditIntegrityError("Compose checkpoint requires a nominal required-work binding")
            required_work.validate_context(session_operation_context)
            if continuation is None:
                checkpoint_sql, checkpoint_projection = required_work.reserve_pair(
                    RequiredWorkSource.COMPOSE_CHECKPOINT_SQL, RequiredWorkSource.COMPOSE_CHECKPOINT_PROJECTION
                )
        try:
            if continuation is not None:
                from elspeth.web.composer.continuation_live_p4 import ContinuationLiveP4
                from elspeth.web.composer.continuation_prepared_p4 import capture_prepared_p4

                if (
                    type(continuation) is not ContinuationLiveP4
                    or continuation.work is not required_work
                    or continuation.inventory.running.session_operation_context is not session_operation_context
                    or type(required_work) is not RequiredWorkBinding
                ):
                    raise AuditIntegrityError("V2 P4 publication changed its retained current phase/ticket owners")
                prepared = capture_prepared_p4(
                    context=session_operation_context,
                    assistant_content=assistant_message.content or "",
                    raw_content=raw_assistant_content,
                    redacted_calls=redacted_assistant_tool_calls,
                    redacted_rows=redacted_tool_rows,
                    revoked_rows=revoked_tool_rows,
                    mutation_ceiling=mutation_ceiling,
                    authority_revocation=authority_revocation,
                    private_tool_evidence=tuple(private_tool_evidence),
                    private_tool_failures=private_tool_failures,
                    rejection_records=build_rejection_records(tool_outcomes),
                    withheld_replies=continuation.withheld_replies,
                    invocations=private_invocations,
                    authorings=private_authorings,
                    parent_composition_state_id=current_state_id,
                    expected_current_state_id=current_state_id,
                    assistant_row_uses_current_dispatch=assistant_row_uses_current_dispatch,
                    crash_pending=crash_pending,
                    env_ref_names=continuation.inventory.names,
                )
                if terminal_completion is None:
                    commit = await continuation.prepare(prepared)
                else:
                    from elspeth.web.composer.continuation_tool_failure import TerminalServiceCompletion

                    if type(terminal_completion) is not TerminalServiceCompletion:
                        raise AuditIntegrityError("terminal publication requires its exact native service completion")
                    terminal_completion.require_p4(continuation)
                    # The terminal P4 owner/SQL/application implementation is a
                    # separate required join. Never encode/clear the live failure
                    # or fall through to the legacy writer when it is unavailable.
                    commit = await continuation.prepare_terminal(prepared, terminal_completion)
                checkpoint_sql, checkpoint_projection = required_work.reserve_pair(
                    RequiredWorkSource.COMPOSE_CHECKPOINT_SQL, RequiredWorkSource.COMPOSE_CHECKPOINT_PROJECTION
                )
                try:
                    continuation.bind_checkpoint_ticket(checkpoint_sql)
                except BaseException as original:
                    # This exact local reservation has not reached the physical
                    # service call. Preserve its real no-submission failure;
                    # the existing outer handler closes the paired projection.
                    try:
                        checkpoint_sql.complete_without_submission(original)
                    except BaseException as cleanup:
                        if cleanup is original:
                            raise
                        raise BaseExceptionGroup("P4 checkpoint binding and no-submission cleanup", (original, cleanup)) from None
                    raise
                physical = await sessions_service.persist_continuation_compose_turn(
                    session_id=session_id,
                    assistant_content=assistant_message.content or "",
                    raw_content=raw_assistant_content,
                    redacted_assistant_tool_calls=redacted_assistant_tool_calls,
                    redacted_tool_rows=redacted_tool_rows,
                    revoked_tool_rows=revoked_tool_rows,
                    mutation_ceiling=mutation_ceiling,
                    authority_revocation=authority_revocation,
                    private_tool_evidence=tuple(private_tool_evidence),
                    private_tool_failures=private_tool_failures,
                    rejection_records=build_rejection_records(tool_outcomes),
                    parent_composition_state_id=current_state_id,
                    expected_current_state_id=current_state_id,
                    writer_principal="compose_loop",
                    plugin_crash_pending=crash_pending,
                    session_operation_context=session_operation_context,
                    continuation=commit,
                    continuation_prepared=prepared,
                    continuation_env_ref_names=continuation.inventory.names,
                    required_work=checkpoint_sql,
                    continuation_observation=continuation.writer_observation,
                )
                audit_outcome = continuation.accept_physical(checkpoint_sql, physical)
            elif checkpoint_sql is None:
                audit_outcome = await sessions_service.persist_compose_turn_async(
                    session_id=session_id,
                    assistant_content=assistant_message.content or "",
                    raw_content=raw_assistant_content,
                    redacted_assistant_tool_calls=redacted_assistant_tool_calls,
                    redacted_tool_rows=redacted_tool_rows,
                    revoked_tool_rows=revoked_tool_rows,
                    mutation_ceiling=mutation_ceiling,
                    authority_revocation=authority_revocation,
                    private_tool_evidence=tuple(private_tool_evidence),
                    private_tool_failures=private_tool_failures,
                    rejection_records=build_rejection_records(tool_outcomes),
                    parent_composition_state_id=current_state_id,
                    expected_current_state_id=current_state_id,
                    writer_principal="compose_loop",
                    plugin_crash_pending=crash_pending,
                    session_operation_context=session_operation_context,
                )
            else:
                audit_outcome = await sessions_service.persist_compose_turn_async(
                    session_id=session_id,
                    assistant_content=assistant_message.content or "",
                    raw_content=raw_assistant_content,
                    redacted_assistant_tool_calls=redacted_assistant_tool_calls,
                    redacted_tool_rows=redacted_tool_rows,
                    revoked_tool_rows=revoked_tool_rows,
                    mutation_ceiling=mutation_ceiling,
                    authority_revocation=authority_revocation,
                    private_tool_evidence=tuple(private_tool_evidence),
                    private_tool_failures=private_tool_failures,
                    rejection_records=build_rejection_records(tool_outcomes),
                    parent_composition_state_id=current_state_id,
                    expected_current_state_id=current_state_id,
                    writer_principal="compose_loop",
                    plugin_crash_pending=crash_pending,
                    session_operation_context=session_operation_context,
                    required_work=checkpoint_sql,
                )
        except AuditIntegrityError as exc:
            if checkpoint_projection is not None and checkpoint_sql is not None and checkpoint_sql.complete:
                checkpoint_projection.complete_without_submission()
            exc.failed_turn = FailedTurnMetadata(
                assistant_message_id=None,
                tool_calls_attempted=len(assistant_tool_calls),
                tool_responses_persisted=0,
            )
            raise
        except BaseException:
            if checkpoint_projection is not None and checkpoint_sql is not None and checkpoint_sql.complete:
                checkpoint_projection.complete_without_submission()
            raise
        if checkpoint_projection is not None:
            checkpoint_projection.begin_projection()
        if type(audit_outcome) is not AuditOutcome:
            failure = AuditIntegrityError("Compose checkpoint returned a foreign audit outcome")
            if checkpoint_projection is not None:
                checkpoint_projection.complete_owned(failure)
            raise failure
        if audit_outcome.assistant_id is not None and audit_outcome.published_assistant_content is None:
            failure = AuditIntegrityError("Committed P4 omitted its actual publication content")
            if checkpoint_projection is not None:
                checkpoint_projection.complete_owned(failure)
            raise failure
        unwind_audit_failed = audit_outcome.unwind_audit_failed
        effect_verification_failure = audit_outcome.continuation_retention_failure
        authority_revocation = audit_outcome.authority_revocation
        redacted_tool_rows = audit_outcome.published_tool_rows
        current_state_id = audit_outcome.current_state_id
        failed_turn = FailedTurnMetadata(
            assistant_message_id=audit_outcome.assistant_id,
            tool_calls_attempted=len(assistant_tool_calls),
            tool_responses_persisted=0 if audit_outcome.assistant_id is None else len(redacted_tool_rows),
        )
        if audit_outcome.assistant_id is None and not crash_pending:
            failure = AuditIntegrityError(
                "persist_compose_turn_async returned unwind_audit_failed without an in-flight plugin crash",
                failed_turn=failed_turn,
            )
            if checkpoint_projection is not None:
                checkpoint_projection.complete_owned(failure)
            raise failure
        if audit_outcome.assistant_id is None and not audit_outcome.unwind_audit_failed:
            failure = AuditIntegrityError(
                "persist_compose_turn_async returned no assistant id without the unwind-audit-failed disposition",
                failed_turn=failed_turn,
            )
            if checkpoint_projection is not None:
                checkpoint_projection.complete_owned(failure)
            raise failure
        persisted_assistant_message_id = audit_outcome.assistant_id
        # Publication selection occurs under the SQL lock. Adopt its actual
        # committed bytes rather than inferring them from the candidate.
        persisted_assistant_content = audit_outcome.published_assistant_content
        # The unwind-failure outcome means the transaction rolled back: no
        # assistant or tool row survived, so the driver must retain the
        # in-flight invocation evidence on the propagated plugin crash.
        persisted_tool_call_turn = audit_outcome.assistant_id is not None
        if turn_evidence is not None and audit_outcome.checkpoint_disposition == "committed":
            try:
                witness = audit_outcome.checkpoint
                if witness is None or audit_outcome.assistant_id is None:
                    raise AuditIntegrityError("New compose checkpoint is missing its committed row witness")
                expected_state = next(
                    (
                        outcome.response.updated_state
                        for outcome in reversed(tool_outcomes)
                        if outcome.post_version > outcome.pre_version
                        and isinstance(outcome.response, ToolResult)
                        and outcome.response.updated_state is not None
                    ),
                    None,
                )
                if expected_state is None:
                    raise AuditIntegrityError("Committed compose checkpoint has no corresponding dispatched state")
                turn_evidence.accept_checkpoint(
                    witness,
                    expected_state=expected_state,
                    tool_call_ids=tuple(item.call.id for item in tool_outcomes),
                    assistant_message_id=audit_outcome.assistant_id,
                )
            except Exception as exc:
                # These rows already committed. Return the real metadata and
                # propagate after the driver records it, never use rollback or
                # unpersisted-suffix handling for a comparison failure.
                if isinstance(exc, AuditIntegrityError):
                    effect_verification_failure = exc
                else:
                    effect_verification_failure = AuditIntegrityError("Committed compose effect verification failed")
                    effect_verification_failure.__cause__ = exc
                effect_verification_failure.failed_turn = failed_turn
                turn_evidence.mark_verification_unavailable()
    try:
        result = _PersistOutcome(
            current_state_id=current_state_id,
            persisted_assistant_message_id=persisted_assistant_message_id,
            persisted_assistant_content=persisted_assistant_content,
            persisted_tool_call_turn=persisted_tool_call_turn,
            persisted_assistant_matches_current_dispatch=(
                persisted_assistant_message_id is not None and assistant_row_uses_current_dispatch and authority_revocation is None
            ),
            unwind_audit_failed=unwind_audit_failed,
            failed_turn=failed_turn,
            redacted_assistant_tool_calls=redacted_assistant_tool_calls,
            redacted_tool_rows=redacted_tool_rows,
            audit_outcome=audit_outcome,
            effect_verification_failure=effect_verification_failure,
            authority_revocation=authority_revocation,
        )
        if continuation is not None:
            from elspeth.web.composer.continuation_live_p4_application import finish_live_p4_application

            continuation.publication = result
            if effects_before is None:
                raise AuditIntegrityError("continuation publication lost its actual before effects owner")
            await finish_live_p4_application(continuation, effects_before=effects_before)
            if terminal_completion is not None and result.unwind_audit_failed:
                continuation.raise_after_publication(
                    AuditIntegrityError("Service failure audit row could not be persisted", failed_turn=result.failed_turn)
                )
    except BaseException as exc:
        if checkpoint_projection is not None:
            checkpoint_projection.complete_owned(exc)
        raise
    if checkpoint_projection is not None:
        checkpoint_projection.complete_owned(effect_verification_failure)
    if continuation is not None:
        continuation.raise_after_publication()
    return result''',
    "p4_application.finish_live_p4_application": r"""async def finish_live_p4_application(
    owner: ContinuationLiveP4, *, effects_before: PersistedTurnEffects
) -> StoredP4Application | StoredTerminalP4Application:
    if (
        type(owner) is not ContinuationLiveP4
        or type(effects_before) is not PersistedTurnEffects
        or owner.prepared is None
        or owner.commit is None
        or owner.publication is None
        or owner.publication.audit_outcome is None
        or owner.committed_outcome is None
        or owner.application is not None
    ):
        raise AuditIntegrityError("warm P4 application lost its actual writer/preparation/projection")
    runtime, inventory = owner.runtime, owner.inventory
    evidence, recorder = runtime.live.turn_evidence, runtime.live.recorder
    effects_after = evidence.effects
    committed = owner.committed_outcome
    if committed.audit_outcome is not owner.publication.audit_outcome:
        raise AuditIntegrityError("warm P4 replaced its actual committed publication")
    value = committed.application
    if type(value) is StoredTerminalP4Application:
        from elspeth.contracts.hashing import canonical_json
        from elspeth.web.composer.continuation_audit_buffer import decode_recorder_snapshot
        from elspeth.web.composer.continuation_terminal_p4_data import decode_terminal_p4_input, decode_terminal_p4_result
        from elspeth.web.sessions.continuation_p4 import ReadmittedP4Result, decode_p4_receipt

        if owner.terminal_completion is None:
            raise AuditIntegrityError("terminal P4 application lost its original native completion")
        owner.terminal_completion.require_p4(owner)
        source = decode_terminal_p4_input(value.input_document, env_ref_names=inventory.names)
        result_data = decode_terminal_p4_result(value.result_document, input_document=value.input_document, env_ref_names=inventory.names)
        receipt, outcome = decode_p4_receipt(canonical_json(result_data.receipt.model_dump(mode="json")), env_ref_names=inventory.names)
        actual = decode_audit_outcome_data(
            encode_audit_outcome_data(owner.publication.audit_outcome, prepared=owner.prepared, env_ref_names=inventory.names),
            prepared=owner.prepared,
            env_ref_names=inventory.names,
        )
        snapshot = recorder.snapshot_for_continuation(drained=runtime.drained)
        retained = decode_recorder_snapshot(source.recorder)
        if (
            receipt.effect_key != owner.commit.effect_key
            or receipt.input_sha256 != owner.commit.input_sha256
            or actual != outcome
            or snapshot.drained != retained.drained
            or any(
                current[: len(previous)] != previous
                for previous, current in (
                    (retained.invocations, snapshot.invocations),
                    (retained.llm_calls, snapshot.llm_calls),
                    (retained.planner_attempts, snapshot.planner_attempts),
                    (retained.withheld_replies, snapshot.withheld_replies),
                    (retained.visible_tool_events, snapshot.visible_tool_events),
                    (retained.private_candidates, snapshot.private_candidates),
                    (retained.private_authorings, snapshot.private_authorings),
                )
            )
        ):
            raise AuditIntegrityError("terminal P4 projection changed its actual prepared publication/recorder")
        following = _project_recorder(snapshot, owner.prepared, ReadmittedP4Result(receipt, outcome, None), env_ref_names=inventory.names)
        if (
            following.drained != result_data.drained
            or evidence.effects is not effects_after
            or recorder.snapshot_for_continuation(drained=runtime.drained) != snapshot
        ):
            raise AuditIntegrityError("terminal P4 projection changed its exact published slices")
        owner.application = value
        owner.runtime = replace(runtime, graph=runtime.graph.with_drained(following.drained), drained=following.drained)
        return value
    if type(value) is not StoredP4Application:
        raise AuditIntegrityError("warm P4 lost its closed application owner")
    result = value.bundle.result
    record = value.bundle.entry.applications[-1]
    if record.effect_key != owner.commit.effect_key or result.receipt.input_sha256 != owner.commit.input_sha256:
        raise AuditIntegrityError("warm P4 changed its actual committed application/result")
    actual = decode_audit_outcome_data(
        encode_audit_outcome_data(owner.publication.audit_outcome, prepared=owner.prepared, env_ref_names=inventory.names),
        prepared=owner.prepared,
        env_ref_names=inventory.names,
    )
    if actual != result.outcome:
        raise AuditIntegrityError("warm P4 projection replaced its actual physical writer outcome")
    snapshot = recorder.snapshot_for_continuation(drained=runtime.drained)
    following = _project_recorder(snapshot, owner.prepared, result, env_ref_names=inventory.names)
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot

    root = decode_continuation_snapshot(value.snapshot.payload_json)
    compose, frame = root.frames[0], root.frames[-1]
    if type(compose) is not ComposeFrame or type(frame) is not ToolBatchFrame:
        raise AuditIntegrityError("warm P4 application changed its actual phase family")
    application = HistoricalP4Application(
        record.checkpoint_id,
        owner.commit.effect_key,
        owner.data,
        result,
        recorder,
        snapshot,
        following,
        effects_before,
        effects_after,
        owner.publication.effect_verification_failure,
    )
    if (
        evidence.effects is not effects_after
        or recorder.snapshot_for_continuation(drained=runtime.drained) != snapshot
        or retained_effects(effects_after) != value.bundle.evidence_after.snapshot.effects
        or following != value.bundle.recorder_after
        or application.effect_key in evidence._historical_p4_applications
    ):
        raise AuditIntegrityError("warm P4 projection differs from its actual checked SQL application")
    evidence._historical_p4_applications[application.effect_key] = application
    data = retain_portable_application_on_live_evidence(
        owner=evidence,
        recorder=recorder,
        application=application,
        bundle=value.bundle,
        env_ref_names=inventory.names,
    )
    owner.data = data
    owner.application = value
    owner.runtime = replace(
        runtime,
        graph=runtime.graph.with_batch_and_drained(data, following.drained),
        drained=following.drained,
    )
    return value""",
    "phases.ContinuationLivePhases._initialize_batch": r"""async def _initialize_batch(self, selected: SelectedContinuationState) -> None:
    runtime, inventory = await self.current(selected)
    result, live = await self._primary_for_phase(runtime, inventory, selected), runtime.live
    frame = selected.stack.frames[-1]
    if type(frame) is not ComposeFrame or frame.pending_response != result.response.reference:
        raise AuditIntegrityError("batch initialization changed its exact original paid response")
    self.service._enforce_tool_call_cap(
        assistant_tool_calls=result.outcome.completion.tool_batch.calls,
        state=live.state,
        initial_version=live.initial_version,
        composition_turns_used=live.composition_turns_used,
        discovery_turns_used=live.discovery_turns_used,
        recorder=live.recorder,
        failed_turn=live.failed_turn,
        persisted_tool_call_turn=live.persisted_tool_call_turn,
    )
    accumulator = BatchAccumulator(live.state, live.last_validation, live.last_runtime_preflight, live.advisor_calls_used)
    batch = ContinuationBatchState(
        live.state, live.last_validation, live.last_runtime_preflight, live.advisor_calls_used, entry_state=live.state
    )
    data = RestoredBatchData(batch, accumulator, result.outcome.completion.tool_batch.calls)
    context = await self._tool_context(runtime)
    await _initialize_continuation_batch_live(
        call_model=result.outcome, ctx=context, acc=accumulator, llm_messages=live.llm_messages, batch=batch
    )
    self.runtime = runtime = replace(runtime, graph=runtime.graph.with_batch(data))
    captured = await capture_continuation_runtime_owners(runtime, selected=selected, inventory=inventory)
    chunk = encode_private_chunk(
        codec="composer.coupled-batch-data.v1",
        canonical=encode_coupled_batch_data(batch=batch, accumulator=accumulator, calls=data.calls, env_ref_names=inventory.names),
        env_ref_names=inventory.names,
    )
    tool = ToolBatchFrame(
        phase="tool_ready", response=result.response.reference, accumulator=chunk.reference, next_tool_index=0, settled_effect_ids=()
    )
    root = captured.stack.model_copy(update={"frames": (*captured.stack.frames, tool)})
    following = CapturedContinuationRuntime(selected, root, _selected_chunks(root, captured.chunks, (chunk,)))
    await self._retain(following, inventory, "batch_initialized")""",
    "phases.ContinuationLivePhases._persist_batch": r"""async def _persist_batch(self, selected: SelectedContinuationState) -> None:
    terminal = self.terminal_completion
    if terminal is None:
        phase_read = await self._current_phase_read(selected)
        runtime, inventory = phase_read.runtime, phase_read.inventory
        result = await self._primary_for_phase(runtime, inventory, selected)
        context = await build_runtime_tool_batch_context(
            runtime, cancellation_requested=self.cancellation_requested, phase_read=phase_read
        )
        call_model = result.outcome
    else:
        terminal.require_native()
        if self.runtime is not terminal.runtime or selected is not terminal.runtime.source.selected or terminal.p4 is not None:
            raise terminal.custody.failure(AuditIntegrityError("terminal P4 cannot replace or replay its actual native completion"))
        runtime = terminal.runtime
        inventory = runtime.materialization.inventory
        context, call_model = terminal.context, terminal.call_model
    data = runtime.graph.batch
    if data is None or data.batch.phase != "dispatch_ready":
        raise AuditIntegrityError("live P4 lost its actual completed batch/provider owners")
    dispatch, advisor_calls = _finish_continuation_batch_live(
        call_model=call_model,
        ctx=context,
        acc=data.accumulator,
        batch=data.batch,
        failure_custody=self.failure_custody,
    )
    live, bindings = runtime.live, runtime.bindings
    self.p4 = owner = ContinuationLiveP4(
        self.authority,
        selected,
        inventory,
        self.work,
        data,
        runtime,
        withheld_replies=live.recorder.withheld_replies[runtime.drained.withheld_replies :],
    )
    if terminal is not None:
        terminal.bind_p4(owner)

    async def publish() -> None:
        deadline_owner = self.work.coordinator.sql_deadline_owner
        if deadline_owner is not None:
            if deadline_owner.running is not inventory.running:
                raise AuditIntegrityError("P4 finishing changed its actual original timing owner")
            # This retained child only finishes the actual existing batch;
            # its SQL owner still proves all current/pre-Stop authority.
            # Spend the original action's reserve without restarting D.
            deadline_owner.begin_settlement()
        original: BaseException | None = None
        try:
            await self.service._persist_turn_audit(
                tool_outcomes=dispatch.tool_outcomes,
                mutation_ceiling=bindings.mutation_ceiling,
                authority_revocation=dispatch.authority_revocation,
                base_state=data.batch.entry_state,
                decoded_args_by_call_id=dispatch.decoded_args_by_call_id,
                private_authorings=dispatch.private_authorings,
                private_invocations=dispatch.private_invocations,
                assistant_message=dispatch.assistant_message,
                raw_assistant_content=dispatch.raw_assistant_content,
                assistant_tool_calls=dispatch.assistant_tool_calls,
                crash_pending=(
                    dispatch.plugin_crash is not None
                    or dispatch.advisor_failure is not None
                    or dispatch.service_failure is not None
                    or (
                        data.batch.retained_failure is not None
                        and data.batch.retained_failure.kind in ("plugin_crash", "advisor_failure")
                    )
                ),
                session_id=bindings.session_id,
                session_operation_context=bindings.session_operation_context,
                current_state_id=live.current_state_id,
                persisted_tool_call_turn=live.persisted_tool_call_turn,
                persisted_assistant_message_id=live.persisted_assistant_message_id,
                persisted_assistant_content=live.persisted_assistant_content,
                ingress=bindings.ingress,
                chat_ingress_inputs=None if bindings.chat_ingress_inputs is None else list(bindings.chat_ingress_inputs),
                advisor_repair_context_introduced=live.advisor_repair_context_introduced,
                required_work=self.work,
                turn_evidence=live.turn_evidence,
                continuation=owner,
                terminal_completion=terminal,
            )
        except BaseException as failure:
            original = failure if self.failure_custody is None else self.failure_custody.failure(failure)
        finally:
            # turn_audit retains actual committed publication before a
            # deferred cancellation or verification failure may escape.
            persist = owner.publication
            if persist is not None:
                live.current_state_id = persist.current_state_id
                live.persisted_assistant_message_id = persist.persisted_assistant_message_id
                live.persisted_assistant_content = persist.persisted_assistant_content
                live.persisted_tool_call_turn, live.failed_turn = persist.persisted_tool_call_turn, persist.failed_turn
                live.advisor_calls_used = advisor_calls
        if owner.publication is not None:
            # The one checkpoint projection reconciled both actual SQL
            # outputs before completing. Never submit a second apply here.
            self.runtime = owner.runtime
            if original is not None:
                raise original
        elif original is not None:
            raise original
        try:
            owner.raise_after_publication()
        except BaseException as failure:
            if self.failure_custody is not None:
                combined = self.failure_custody.failure(failure)
                if combined is failure:
                    raise
                raise combined from combined.__cause__
            raise
        custody = self.failure_custody
        if custody is not None and custody.completed:
            if custody.wrapper is not None:
                raise custody.wrapper from custody.original
            if custody.original is not None:
                raise custody.original

    critical = self.lease.create_continuation_stage(publish(), name="continuation-required-p4")
    await self.lease.join_continuation_stage(critical)""",
    "p4.ContinuationLiveP4.__post_init__": r"""def __post_init__(self) -> None:
    if (
        type(self.authority) is not ComposerAsyncOperationAuthority
        or type(self.selected) is not SelectedContinuationState
        or type(self.inventory) is not CurrentContinuationEnvInventory
        or type(self.work) is not RequiredWorkBinding
        or type(self.data) is not RestoredBatchData
        or type(self.runtime) is not RestoredContinuationRuntime
        or self.runtime.graph.batch is not self.data
        or self.runtime.bindings.required_work is not self.work
        or self.work.running is None
        or self.inventory.running is not self.work.running
    ):
        raise AuditIntegrityError("live P4 lost its actual current phase owners")
    self.work.validate_context(self.inventory.running.session_operation_context)
""",
    "p4.ContinuationLiveP4._retain_cancellations": r"""def _retain_cancellations[T](self, outcome: RequiredSQLFinishOnce[T]) -> None:
    for cancellation in outcome.deferred_cancellations:
        if all(cancellation is not previous for previous in self.deferred_cancellations):
            self.deferred_cancellations.append(cancellation)
""",
    "p4.ContinuationLiveP4.prepare": r"""async def prepare(self, prepared: PreparedP4Value) -> ContinuationOwnerCommit:
    if self.preparation is not None or self.prepared is not None:
        raise AuditIntegrityError("live P4 preparation cannot replace a retained physical outcome")
    names = await _read_current_env_names(
        self.runtime.materialization.service._advisor_checkpoint, self.selected.stack.logical.session_id
    )
    self.inventory = CurrentContinuationEnvInventory(self.inventory.running, self.inventory.selection, names)
    pending = replace(
        self.data.batch, phase="p4_pending", p4_input_json=encode_prepared_p4(prepared, env_ref_names=self.inventory.names)
    )
    chunk = encode_p4_pending_batch_chunk(
        batch=pending, accumulator=self.data.accumulator, calls=self.data.calls, env_ref_names=self.inventory.names
    )
    members = prepared_p4_members(prepared, env_ref_names=self.inventory.names)
    recorded = self.runtime.live.recorder.invocations
    linked = self.runtime.live.recorder.visible_tool_events
    for expected, row in zip(members.invocations, members.redacted_rows, strict=True):
        actual = tuple(invocation for invocation in recorded if invocation.tool_call_id == expected.tool_call_id)
        if len(actual) != 1 or actual[0] != expected:
            raise AuditIntegrityError("P4 redaction lost its actual recorded invocation")
        if row.visible_event is not None:
            previous = tuple(event for invocation, event in linked if invocation is actual[0])
            if previous and previous != (row.visible_event,):
                raise AuditIntegrityError("P4 redaction replaced its actual visible event")
            if not previous:
                self.runtime.live.recorder.record_visible_tool_event(actual[0], row.visible_event)
    pending_data = RestoredBatchData(pending, self.data.accumulator, self.data.calls)
    runtime = replace(self.runtime, graph=self.runtime.graph.with_batch(pending_data))

    def read_source() -> ContinuationRuntimeSource:
        try:
            return self.authority.read_continuation_runtime(self.inventory.running, selected=self.selected, inventory=self.inventory)
        except ComposerOperationCancelledDuringTurn:
            # A real durable Stop selects the separate read facet. It must
            # prove immutable pre-Stop issue ordering again; no caller flag
            # or old frontier grants the pending P4 write.
            return self.authority.read_continuation_owed_batch_runtime(
                self.inventory.running, selected=self.selected, inventory=self.inventory
            )

    def capture() -> CapturedContinuationRuntime:
        source = read_source()
        captured = _capture_owners(runtime, source=source, inventory=self.inventory)
        actual = read_source()
        if actual.selected != source.selected or actual.bootstrap != source.bootstrap or actual.evidence != source.evidence:
            raise AuditIntegrityError("P4 capture changed its complete current source during retention")
        self.captured = captured
        return captured

    captured = await _registered_read(self.work, CapturedContinuationRuntime, capture)
    top = captured.stack.frames[-1]
    if type(top) is not ToolBatchFrame:
        raise AuditIntegrityError("P4 capture lost its actual completed batch frame")
    sql, projection = self.work.reserve_pair(
        RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION
    )
    try:
        outcome = await run_required_sql_finish_once(
            sql,
            self.authority.prepare_continuation_p4,
            self.inventory.running,
            expected_checkpoint_id=self.selected.checkpoint_id,
            expected_cursor=self.selected.stack.cursor,
            pending_batch=chunk,
            env_ref_names=self.inventory.names,
            captured=captured,
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    self.preparation = outcome
    self._retain_cancellations(outcome)
    try:
        self.work.coordinator.validate_continuation_sql_outcome(
            ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_STATE_SQL, actual_outcome=outcome
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        self.raise_after_publication(outcome.error)
    if type(outcome) is not RequiredSQLReturned:
        projection.complete_without_submission()
        raise AuditIntegrityError("live P4 preparation returned another physical outcome")
    projection.begin_projection()
    try:
        if type(outcome.value) is not ContinuationOwnerCommit or outcome.value.running is not self.inventory.running:
            raise AuditIntegrityError("live P4 preparation changed its actual current owner")
        self.commit, self.prepared = outcome.value, prepared
        self.writer_observation = P4WriterObservation(outcome.value, prepared)
        self.data.batch.phase, self.data.batch.p4_input_json = pending.phase, pending.p4_input_json
    except BaseException as error:
        projection.complete_owned(error)
        raise
    projection.complete_owned()
    return outcome.value""",
    "p4.ContinuationLiveP4.bind_checkpoint_ticket": r'''def bind_checkpoint_ticket(self, ticket: RequiredWorkTicket) -> None:
    """Capture this P4's actual reservation before its physical invocation.

    This pointer grants no write. The service and transaction still prove
    source/current authority, and acceptance separately proves its Future.
    """
    coordinator = self.work.coordinator
    with coordinator._lock:
        if self._checkpoint_ticket is not None or self.physical is not None:
            raise AuditIntegrityError("live P4 checkpoint ticket is already bound or observed")
        coordinator._registered(ticket, RequiredWorkSource.COMPOSE_CHECKPOINT_SQL)
        running = self.inventory.running
        self.work.validate_context(running.session_operation_context)
        if (
            self.work.running is not running
            or self.commit is None
            or self.commit.running is not running
            or self.prepared is None
            or self.writer_observation is None
            or self.writer_observation.commit is not self.commit
            or self.writer_observation.prepared is not self.prepared
            or ticket.key.authority.context != running.session_operation_context
            or ticket.key.authority.durable_operation_id != running.claim.operation_id
            or ticket.key.authority.claim_attempt != running.claim.attempt
            or ticket.key.transition_ordinal != self.work.transition_ordinal
            or ticket.key.semantic_ordinal != self.work.semantic_ordinal
            or ticket.key.subphase is not RequiredWorkSubphase.SQL_INITIAL
            or ticket.key.sql_attempt_ordinal != 0
        ):
            raise AuditIntegrityError("live P4 checkpoint ticket crossed its actual prepared scope")
        with ticket._lock:
            if (
                ticket._complete
                or ticket._future is not None
                or ticket._submission_unknown
                or ticket._projection_started
                or ticket._finish_once_handoff is not None
            ):
                raise AuditIntegrityError("live P4 checkpoint binding must precede physical submission")
            self._checkpoint_ticket = ticket
''',
    "p4.ContinuationLiveP4.accept_physical": r"""def accept_physical(self, ticket: RequiredWorkTicket, outcome: RequiredSQLFinishOnce[CommittedContinuationP4]) -> AuditOutcome:
    with self.work.coordinator._lock:
        if self.physical is not None:
            raise AuditIntegrityError("live P4 cannot replace its retained physical SQL result")
        self.physical = outcome
        self._retain_cancellations(outcome)
        if self.writer_observation is not None:
            self.deferred_failures.extend(self.writer_observation.prior_errors)
        if self.writer_observation is not None and self.writer_observation.original_error is not None:
            self.deferred_failures.append(self.writer_observation.original_error)
        if self._checkpoint_ticket is None or ticket is not self._checkpoint_ticket:
            raise AuditIntegrityError("live P4 replaced its actual bound checkpoint ticket")
        self.work.coordinator.validate_compose_checkpoint_sql_outcome(ticket=ticket, actual_outcome=outcome)
    if type(outcome) is RequiredSQLRaised:
        self.raise_after_publication(outcome.error)
    if type(outcome) is not RequiredSQLReturned or type(outcome.value) is not CommittedContinuationP4:
        raise AuditIntegrityError("live P4 returned a foreign physical publication")
    observed = self.writer_observation
    if (
        observed is None
        or not observed.committed
        or outcome.value.audit_outcome is not observed.candidate
        or outcome.value.application is not observed.application_candidate
    ):
        raise AuditIntegrityError("live P4 replaced its actual committing owner's outputs")
    self.committed_outcome = outcome.value
    return outcome.value.audit_outcome
""",
    "p4.ContinuationLiveP4.raise_after_publication": r"""def raise_after_publication(self, original: BaseException | None = None) -> None:
    errors: list[BaseException] = [] if original is None else [original]
    errors.extend(failure for failure in self.deferred_failures if failure is not original)
    errors.extend(cancellation for cancellation in self.deferred_cancellations if cancellation is not original)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Live P4 retained outcomes", errors)
""",
    "graph.RestoredTurnEvidence": r'''class RestoredTurnEvidence:
    """Original live evidence owners with closed bindings, never a frozen DATA snapshot."""

    __slots__ = (
        "_sealed",
        "applications",
        "batch",
        "drained",
        "evidence",
        "messages",
        "recorder",
        "selected_audit",
        "selected_dialect",
    )

    evidence: TurnEvidence
    messages: list[dict[str, Any]]
    recorder: BufferingRecorder
    drained: RecorderDrainCursors
    batch: RestoredBatchData | None
    applications: PortableP4ApplicationEntry | None
    selected_audit: PortableDispatchAudit | None
    selected_dialect: ToolContractDialect | None
    _sealed: bool

    def __new__(
        cls,
        evidence: TurnEvidence,
        messages: list[dict[str, object]],
        recorder: BufferingRecorder,
        drained: RecorderDrainCursors,
        batch: RestoredBatchData | None = None,
        applications: PortableP4ApplicationEntry | None = None,
        selected_audit: PortableDispatchAudit | None = None,
        selected_dialect: ToolContractDialect | None = None,
    ) -> Self:
        owner = object.__new__(cls)
        object.__setattr__(owner, "_sealed", False)
        return owner

    def __init__(
        self,
        evidence: TurnEvidence,
        messages: list[dict[str, object]],
        recorder: BufferingRecorder,
        drained: RecorderDrainCursors,
        batch: RestoredBatchData | None = None,
        applications: PortableP4ApplicationEntry | None = None,
        selected_audit: PortableDispatchAudit | None = None,
        selected_dialect: ToolContractDialect | None = None,
    ) -> None:
        if self._sealed:
            raise FrozenInstanceError("RestoredTurnEvidence cannot be reinitialized")
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "messages", messages)
        object.__setattr__(self, "recorder", recorder)
        object.__setattr__(self, "drained", drained)
        object.__setattr__(self, "batch", batch)
        object.__setattr__(self, "applications", applications)
        object.__setattr__(self, "selected_audit", selected_audit)
        object.__setattr__(self, "selected_dialect", selected_dialect)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        raise FrozenInstanceError("RestoredTurnEvidence bindings cannot be reassigned")

    def __delattr__(self, name: str) -> None:
        raise FrozenInstanceError("RestoredTurnEvidence bindings cannot be deleted")

    def __eq__(self, other: object) -> bool:
        if type(self) is not RestoredTurnEvidence or type(other) is not RestoredTurnEvidence:
            return NotImplemented
        return (
            self.evidence,
            self.messages,
            self.recorder,
            self.drained,
            self.batch,
            self.applications,
            self.selected_audit,
            self.selected_dialect,
        ) == (
            other.evidence,
            other.messages,
            other.recorder,
            other.drained,
            other.batch,
            other.applications,
            other.selected_audit,
            other.selected_dialect,
        )

    def with_batch(self, batch: RestoredBatchData) -> RestoredTurnEvidence:
        return RestoredTurnEvidence(
            self.evidence,
            self.messages,
            self.recorder,
            self.drained,
            batch,
            self.applications,
            self.selected_audit,
            self.selected_dialect,
        )

    def with_drained(self, drained: RecorderDrainCursors) -> RestoredTurnEvidence:
        return RestoredTurnEvidence(
            self.evidence,
            self.messages,
            self.recorder,
            drained,
            self.batch,
            self.applications,
            self.selected_audit,
            self.selected_dialect,
        )

    def with_batch_and_drained(self, batch: RestoredBatchData, drained: RecorderDrainCursors) -> RestoredTurnEvidence:
        return RestoredTurnEvidence(
            self.evidence,
            self.messages,
            self.recorder,
            drained,
            batch,
            self.applications,
            self.selected_audit,
            self.selected_dialect,
        )

    def with_selected_dispatch(self, audit: PortableDispatchAudit, dialect: ToolContractDialect) -> RestoredTurnEvidence:
        return RestoredTurnEvidence(
            self.evidence,
            self.messages,
            self.recorder,
            self.drained,
            self.batch,
            self.applications,
            audit,
            dialect,
        )

    def without_selected_dispatch(self) -> RestoredTurnEvidence:
        return RestoredTurnEvidence(
            self.evidence,
            self.messages,
            self.recorder,
            self.drained,
            self.batch,
            self.applications,
            None,
            None,
        )''',
    "families._ToolResultFamilySource": r'''class _ToolResultFamilySource:
    """Closed bindings to the original live result and message owners."""

    __slots__ = (
        "_sealed",
        "call",
        "message",
        "recipe",
        "result",
    )

    recipe: ToolResultMessageRecipe
    call: _AdmittedToolCall
    result: ToolResult
    message: dict[str, Any]
    _sealed: bool

    def __new__(
        cls,
        recipe: ToolResultMessageRecipe,
        call: _AdmittedToolCall,
        result: ToolResult,
        message: dict[str, object],
    ) -> Self:
        owner = object.__new__(cls)
        object.__setattr__(owner, "_sealed", False)
        return owner

    def __init__(
        self,
        recipe: ToolResultMessageRecipe,
        call: _AdmittedToolCall,
        result: ToolResult,
        message: dict[str, object],
    ) -> None:
        if self._sealed:
            raise FrozenInstanceError("_ToolResultFamilySource cannot be reinitialized")
        object.__setattr__(self, "recipe", recipe)
        object.__setattr__(self, "call", call)
        object.__setattr__(self, "result", result)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        raise FrozenInstanceError("_ToolResultFamilySource bindings cannot be reassigned")

    def __delattr__(self, name: str) -> None:
        raise FrozenInstanceError("_ToolResultFamilySource bindings cannot be deleted")

    def __eq__(self, other: object) -> bool:
        if type(self) is not _ToolResultFamilySource or type(other) is not _ToolResultFamilySource:
            return NotImplemented
        return (
            self.recipe,
            self.call,
            self.result,
            self.message,
        ) == (
            other.recipe,
            other.call,
            other.result,
            other.message,
        )''',
    "families._ToolFeedbackFamilySource": r'''class _ToolFeedbackFamilySource:
    """Closed bindings to the original live feedback and message owners."""

    __slots__ = (
        "_sealed",
        "call",
        "invocation",
        "message",
        "outcome",
        "recipe",
    )

    recipe: ToolFeedbackMessageRecipe
    call: _AdmittedToolCall
    outcome: _ToolOutcome
    invocation: ComposerToolInvocation
    message: dict[str, Any]
    _sealed: bool

    def __new__(
        cls,
        recipe: ToolFeedbackMessageRecipe,
        call: _AdmittedToolCall,
        outcome: _ToolOutcome,
        invocation: ComposerToolInvocation,
        message: dict[str, object],
    ) -> Self:
        owner = object.__new__(cls)
        object.__setattr__(owner, "_sealed", False)
        return owner

    def __init__(
        self,
        recipe: ToolFeedbackMessageRecipe,
        call: _AdmittedToolCall,
        outcome: _ToolOutcome,
        invocation: ComposerToolInvocation,
        message: dict[str, object],
    ) -> None:
        if self._sealed:
            raise FrozenInstanceError("_ToolFeedbackFamilySource cannot be reinitialized")
        object.__setattr__(self, "recipe", recipe)
        object.__setattr__(self, "call", call)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "invocation", invocation)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        raise FrozenInstanceError("_ToolFeedbackFamilySource bindings cannot be reassigned")

    def __delattr__(self, name: str) -> None:
        raise FrozenInstanceError("_ToolFeedbackFamilySource bindings cannot be deleted")

    def __eq__(self, other: object) -> bool:
        if type(self) is not _ToolFeedbackFamilySource or type(other) is not _ToolFeedbackFamilySource:
            return NotImplemented
        return (
            self.recipe,
            self.call,
            self.outcome,
            self.invocation,
            self.message,
        ) == (
            other.recipe,
            other.call,
            other.outcome,
            other.invocation,
            other.message,
        )''',
    "families._RestoredFamilyCohort": r'''class _RestoredFamilyCohort:
    """One reconstruction's original mutable message aliases; never a write grant."""

    __slots__ = (
        "_sealed",
        "feedback",
        "messages",
        "sources",
    )

    messages: tuple[dict[str, Any], ...]
    feedback: tuple[tuple[ToolFeedbackMessageRecipe, _ToolFeedbackFamilySource], ...]
    sources: tuple[_ToolFeedbackFamilySource, ...]
    _sealed: bool

    def __new__(
        cls,
        messages: tuple[dict[str, Any], ...],
        feedback: tuple[tuple[ToolFeedbackMessageRecipe, _ToolFeedbackFamilySource], ...],
        sources: tuple[_ToolFeedbackFamilySource, ...],
    ) -> Self:
        owner = object.__new__(cls)
        object.__setattr__(owner, "_sealed", False)
        return owner

    def __init__(
        self,
        messages: tuple[dict[str, Any], ...],
        feedback: tuple[tuple[ToolFeedbackMessageRecipe, _ToolFeedbackFamilySource], ...],
        sources: tuple[_ToolFeedbackFamilySource, ...],
    ) -> None:
        if self._sealed:
            raise FrozenInstanceError("_RestoredFamilyCohort cannot be reinitialized")
        object.__setattr__(self, "messages", messages)
        object.__setattr__(self, "feedback", feedback)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        raise FrozenInstanceError("_RestoredFamilyCohort bindings cannot be reassigned")

    def __delattr__(self, name: str) -> None:
        raise FrozenInstanceError("_RestoredFamilyCohort bindings cannot be deleted")

    def __eq__(self, other: object) -> bool:
        if type(self) is not _RestoredFamilyCohort or type(other) is not _RestoredFamilyCohort:
            return NotImplemented
        return (
            self.messages,
            self.feedback,
            self.sources,
        ) == (
            other.messages,
            other.feedback,
            other.sources,
        )''',
    "producers._FeedbackMessageProvenance": r'''class _FeedbackMessageProvenance:
    """Original live alias/source facts with closed bindings and no enclosing wire copy."""

    __slots__ = (
        "_sealed",
        "authoring",
        "body",
        "call",
        "call_id",
        "invocation",
        "keys",
        "message",
        "outcome",
        "recipe",
        "role",
        "selector",
    )

    message: dict[str, Any]
    recipe: ToolFeedbackMessageRecipe
    keys: tuple[str, ...]
    role: str
    call_id: str
    body: str
    selector: tuple[str, str, int, int, str, str]
    call: _AdmittedToolCall
    invocation: ComposerToolInvocation
    authoring: PrivateToolAuthoring
    outcome: _ToolOutcome | None
    _sealed: bool

    def __new__(
        cls,
        message: dict[str, object],
        recipe: ToolFeedbackMessageRecipe,
        keys: tuple[str, ...],
        role: str,
        call_id: str,
        body: str,
        selector: tuple[str, str, int, int, str, str],
        call: _AdmittedToolCall,
        invocation: ComposerToolInvocation,
        authoring: PrivateToolAuthoring,
        outcome: _ToolOutcome | None,
    ) -> Self:
        owner = object.__new__(cls)
        object.__setattr__(owner, "_sealed", False)
        return owner

    def __init__(
        self,
        message: dict[str, object],
        recipe: ToolFeedbackMessageRecipe,
        keys: tuple[str, ...],
        role: str,
        call_id: str,
        body: str,
        selector: tuple[str, str, int, int, str, str],
        call: _AdmittedToolCall,
        invocation: ComposerToolInvocation,
        authoring: PrivateToolAuthoring,
        outcome: _ToolOutcome | None,
    ) -> None:
        if self._sealed:
            raise FrozenInstanceError("_FeedbackMessageProvenance cannot be reinitialized")
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "recipe", recipe)
        object.__setattr__(self, "keys", keys)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "call_id", call_id)
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "selector", selector)
        object.__setattr__(self, "call", call)
        object.__setattr__(self, "invocation", invocation)
        object.__setattr__(self, "authoring", authoring)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        raise FrozenInstanceError("_FeedbackMessageProvenance bindings cannot be reassigned")

    def __delattr__(self, name: str) -> None:
        raise FrozenInstanceError("_FeedbackMessageProvenance bindings cannot be deleted")

    def __eq__(self, other: object) -> bool:
        if type(self) is not _FeedbackMessageProvenance or type(other) is not _FeedbackMessageProvenance:
            return NotImplemented
        return (
            self.message,
            self.recipe,
            self.keys,
            self.role,
            self.call_id,
            self.body,
            self.selector,
            self.call,
            self.invocation,
            self.authoring,
            self.outcome,
        ) == (
            other.message,
            other.recipe,
            other.keys,
            other.role,
            other.call_id,
            other.body,
            other.selector,
            other.call,
            other.invocation,
            other.authoring,
            other.outcome,
        )

    def with_outcome(self, outcome: _ToolOutcome) -> _FeedbackMessageProvenance:
        return _FeedbackMessageProvenance(
            self.message,
            self.recipe,
            self.keys,
            self.role,
            self.call_id,
            self.body,
            self.selector,
            self.call,
            self.invocation,
            self.authoring,
            outcome,
        )''',
    "producers.CurrentContinuationMessageProducers._bind_feedback_outcome": r'''def _bind_feedback_outcome(self, publication: ContinuationToolMessagePublication, outcome: _ToolOutcome) -> None:
    """Complete a private session-inner DATA pair, once, before capture."""
    if type(publication) is not ContinuationToolMessagePublication or publication.producers is not self:
        raise AuditIntegrityError("feedback completion lost its original publication owner")
    publication.require_call(
        recorder=publication.recorder,
        audit=publication.call.audit,
        tool_name=publication.call.tool_name,
        tool_call_id=publication.call.tool_call.id,
        messages=publication.messages,
    )
    call, batch = publication.call, publication.batch
    if (
        type(outcome) is not _ToolOutcome
        or len(batch.tool_outcomes) != call.index + 1
        or batch.tool_outcomes[call.index] is not outcome
        or outcome.call is not call.tool_call
    ):
        raise AuditIntegrityError("feedback completion replaced its actual outer outcome/list identity")
    entries = tuple((index, item) for index, item in enumerate(self._feedback_produced) if item.call is call.tool_call)
    if len(entries) != 1:
        raise AuditIntegrityError("feedback completion has no unique actual inner producer")
    index, record = entries[0]
    top = self.selected.stack.frames[-1]
    if (
        record.outcome is not None
        or type(top) is not ToolBatchFrame
        or top.phase != "effect_reconciling"
        or top.response != record.recipe.response
        or top.next_tool_index != call.index
        or record.recipe.tool_index != call.index
        or record.authoring is not batch.private_authorings_by_call[call.tool_call.id]
        or sum(item is record.invocation for item in publication.recorder.invocations) != 1
        or sum(item is record.message for item in publication.messages) != 1
    ):
        raise AuditIntegrityError("feedback completion changed or duplicated its original inner source")
    bound = record.with_outcome(outcome)
    self._feedback_view(bound)
    self._feedback_produced[index] = bound''',
    "failure.ContinuationToolFailureCustody.promote_completed_service_failure": r'''def promote_completed_service_failure(self, original: _ServiceFailure) -> None:
    """Close only the actual handler's audited refusal; retain no descriptor."""
    self.observe_primary(original)
    observation = self.observation
    invocation = observation.stored
    if (
        not isinstance(original, _ServiceFailure)
        or self.completed
        or self.errors
        or self.original is not original
        or observation.owner is not self
        or self.call.audit is not observation.audit
        or not observation.execution_failed
        or observation.returned
        or invocation is None
        or not observation.appended
        or not self.batch.tool_outcomes
        or self.batch.retained_failure is not None
        or self.batch.plugin_crash is not None
        or self.batch.advisor_failure is not None
    ):
        raise AuditIntegrityError("service completion lost its one actual handler origin/audit")
    outcome = self.batch.tool_outcomes[-1]
    if outcome.call is not self.call.tool_call:
        raise AuditIntegrityError("service completion changed its actual issued outcome")
    _require_service_failure_output(outcome, invocation)
    self.invocation = invocation
    self.batch.service_failure = original
    self.batch.stop_issued_batch = True
    self.completed = True''',
    "failure.TerminalServiceCompletion": r'''@dataclass(slots=True)
class TerminalServiceCompletion:
    """One live issued completion; neither portable DATA nor a SQL permission.

    Seal before a P4 owner exists. The same actual owner binds later and must
    reprove its immutable issue/prefix/fence in authoritative terminal P4 SQL.
    """

    custody: ContinuationToolFailureCustody = field(repr=False)
    runtime: RestoredContinuationRuntime = field(repr=False)
    context: ToolBatchContext = field(repr=False)
    call_model: _CallModelOutcome = field(repr=False)
    issued_effect_key: str
    p4: ContinuationLiveP4 | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.require_native()

    def require_native(self) -> None:
        from elspeth.web.composer.continuation import ToolBatchFrame
        from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime
        from elspeth.web.composer.tool_batch import ToolBatchContext

        custody, runtime = self.custody, self.runtime
        if (
            type(custody) is not ContinuationToolFailureCustody
            or type(runtime) is not RestoredContinuationRuntime
            or type(self.context) is not ToolBatchContext
            or type(self.call_model) is not _CallModelOutcome
            or runtime.graph.batch is None
        ):
            raise AuditIntegrityError("terminal service completion requires its actual native owners")
        data = runtime.graph.batch
        batch, call, original = data.batch, custody.call, custody.original
        top = runtime.source.selected.stack.frames[-1]
        if (
            not isinstance(original, _ServiceFailure)
            or not custody.completed
            or custody.errors
            or custody.wrapper is not None
            or custody.batch is not batch
            or custody.calls is not data.calls
            or self.call_model.completion.tool_batch.calls is not data.calls
            or batch.service_failure is not original
            or batch.retained_failure is not None
            or batch.phase != "dispatch_ready"
            or batch.current_call is not None
            or call.phase != "applied"
            or batch.next_tool_index != call.index + 1
            or not batch.stop_issued_batch
            or len(batch.tool_outcomes) != batch.next_tool_index
            or batch.tool_outcomes[-1].call is not call.tool_call
            or custody.invocation is None
            or custody.observation.stored is not custody.invocation
            or not custody.observation.appended
            or not custody.observation.execution_failed
            or custody.observation.returned
            or self.context.recorder is not custody.recorder
            or runtime.live.recorder is not custody.recorder
            or runtime.bindings.required_work is not custody.work
            or runtime.materialization.inventory.running is not custody.running
            or self.context.session_operation_context is not custody.running.session_operation_context
            or type(top) is not ToolBatchFrame
            or top.phase != "effect_reconciling"
            or top.next_tool_index != call.index
            or top.response != custody.response.reference
            or self.issued_effect_key != f"tool:{custody.response.reference.digest}:{call.index}"
        ):
            raise AuditIntegrityError("terminal service completion changed its actual issue/prefix/native origin")
        _require_service_failure_output(batch.tool_outcomes[-1], custody.invocation)
        if not any(invocation is custody.invocation for invocation in custody.recorder.invocations):
            raise AuditIntegrityError("terminal service completion lost its actual recorded invocation")

    def bind_p4(self, owner: ContinuationLiveP4) -> None:
        if self.p4 is not None:
            raise AuditIntegrityError("terminal service completion cannot replace its one actual P4 owner")
        self._require_p4_owners(owner)
        self.p4 = owner

    def _require_p4_owners(self, owner: ContinuationLiveP4) -> None:
        from elspeth.web.composer.continuation_live_p4 import ContinuationLiveP4

        self.require_native()
        if (
            type(owner) is not ContinuationLiveP4
            or owner.runtime is not self.runtime
            or owner.data is not self.runtime.graph.batch
            or owner.work is not self.custody.work
            or owner.inventory.running is not self.custody.running
            or owner.selected is not self.runtime.source.selected
        ):
            raise AuditIntegrityError("terminal service completion changed its actual P4/context owners")

    def require_p4(self, owner: ContinuationLiveP4) -> None:
        self._require_p4_owners(owner)
        if self.p4 is not owner:
            raise AuditIntegrityError("terminal service P4 does not own this native completion")''',
    "failure._require_service_failure_output": r"""def _require_service_failure_output(outcome: _ToolOutcome, invocation: ComposerToolInvocation) -> None:
    from elspeth.web.composer.audit_storage import _validated_canonical_mapping_pair, _validated_invocation_arguments

    _validated_invocation_arguments(invocation)
    payload = outcome.response
    if (
        not isinstance(payload, Mapping)
        or set(payload) != {"success", "status", "reason"}
        or payload["success"] is not False
        or type(payload["status"]) is not str
        or payload["status"] != "SERVICE_FAILURE"
        or type(payload["reason"]) is not str
        or payload["reason"] != "service_failure"
        or outcome.error_class is not None
        or outcome.error_category is not None
        or outcome.error_message is not None
        or outcome.pre_version != outcome.post_version
        or invocation.status is not ComposerToolStatus.SUCCESS
        or invocation.private_failure is not None
        or invocation.error_class is not None
        or invocation.error_message is not None
        or invocation.error_category is not None
        or invocation.tool_call_id != outcome.call.id
        or invocation.tool_name != outcome.call.function.name
        or invocation.version_before != outcome.pre_version
        or invocation.version_after != outcome.post_version
        or invocation.strict_sent is not outcome.strict_sent
        or invocation.wire_conformant is not outcome.wire_conformant
        or invocation.private_authoring is None
        or invocation.private_authoring.raw_arguments != outcome.call.function.arguments
        or invocation.result_canonical != canonical_json(payload)
        or invocation.result_hash is None
    ):
        raise AuditIntegrityError("service completion lost its exact controlled result/invocation")
    _validated_canonical_mapping_pair(invocation.result_canonical, invocation.result_hash, label="service refusal original result")""",
    "failure.TerminalServiceCompletion.__post_init__": r"""def __post_init__(self) -> None:
    self.require_native()""",
    "failure.TerminalServiceCompletion.require_native": r"""def require_native(self) -> None:
    from elspeth.web.composer.continuation import ToolBatchFrame
    from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime
    from elspeth.web.composer.tool_batch import ToolBatchContext

    custody, runtime = self.custody, self.runtime
    if (
        type(custody) is not ContinuationToolFailureCustody
        or type(runtime) is not RestoredContinuationRuntime
        or type(self.context) is not ToolBatchContext
        or type(self.call_model) is not _CallModelOutcome
        or runtime.graph.batch is None
    ):
        raise AuditIntegrityError("terminal service completion requires its actual native owners")
    data = runtime.graph.batch
    batch, call, original = data.batch, custody.call, custody.original
    top = runtime.source.selected.stack.frames[-1]
    if (
        not isinstance(original, _ServiceFailure)
        or not custody.completed
        or custody.errors
        or custody.wrapper is not None
        or custody.batch is not batch
        or custody.calls is not data.calls
        or self.call_model.completion.tool_batch.calls is not data.calls
        or batch.service_failure is not original
        or batch.retained_failure is not None
        or batch.phase != "dispatch_ready"
        or batch.current_call is not None
        or call.phase != "applied"
        or batch.next_tool_index != call.index + 1
        or not batch.stop_issued_batch
        or len(batch.tool_outcomes) != batch.next_tool_index
        or batch.tool_outcomes[-1].call is not call.tool_call
        or custody.invocation is None
        or custody.observation.stored is not custody.invocation
        or not custody.observation.appended
        or not custody.observation.execution_failed
        or custody.observation.returned
        or self.context.recorder is not custody.recorder
        or runtime.live.recorder is not custody.recorder
        or runtime.bindings.required_work is not custody.work
        or runtime.materialization.inventory.running is not custody.running
        or self.context.session_operation_context is not custody.running.session_operation_context
        or type(top) is not ToolBatchFrame
        or top.phase != "effect_reconciling"
        or top.next_tool_index != call.index
        or top.response != custody.response.reference
        or self.issued_effect_key != f"tool:{custody.response.reference.digest}:{call.index}"
    ):
        raise AuditIntegrityError("terminal service completion changed its actual issue/prefix/native origin")
    _require_service_failure_output(batch.tool_outcomes[-1], custody.invocation)
    if not any(invocation is custody.invocation for invocation in custody.recorder.invocations):
        raise AuditIntegrityError("terminal service completion lost its actual recorded invocation")""",
    "failure.TerminalServiceCompletion.bind_p4": r"""def bind_p4(self, owner: ContinuationLiveP4) -> None:
    if self.p4 is not None:
        raise AuditIntegrityError("terminal service completion cannot replace its one actual P4 owner")
    self._require_p4_owners(owner)
    self.p4 = owner""",
    "failure.TerminalServiceCompletion._require_p4_owners": r"""def _require_p4_owners(self, owner: ContinuationLiveP4) -> None:
    from elspeth.web.composer.continuation_live_p4 import ContinuationLiveP4

    self.require_native()
    if (
        type(owner) is not ContinuationLiveP4
        or owner.runtime is not self.runtime
        or owner.data is not self.runtime.graph.batch
        or owner.work is not self.custody.work
        or owner.inventory.running is not self.custody.running
        or owner.selected is not self.runtime.source.selected
    ):
        raise AuditIntegrityError("terminal service completion changed its actual P4/context owners")""",
    "failure.TerminalServiceCompletion.require_p4": r"""def require_p4(self, owner: ContinuationLiveP4) -> None:
    self._require_p4_owners(owner)
    if self.p4 is not owner:
        raise AuditIntegrityError("terminal service P4 does not own this native completion")""",
}

OWNER_CONSTRUCTORS: dict[str, str] = {
    "producers.CurrentContinuationMessageProducers": r"""@dataclass(slots=True)
class CurrentContinuationMessageProducers:
    service: ComposerServiceImpl = field(repr=False)
    authority: ComposerAsyncOperationAuthority = field(repr=False)
    selected: SelectedContinuationState = field(repr=False)
    inventory: CurrentContinuationEnvInventory = field(repr=False)
    inputs: _CurrentProducerInputs | _ReadyProducerInputs = field(repr=False)
    finish_read: ContinuationFinishRead | None = field(default=None, repr=False)
    _produced: list[tuple[Mapping[str, Any], ContinuationMessageRecipe, str]] = field(default_factory=list, init=False, repr=False)
    _schema_inputs: list[tuple[_AdmittedToolCall, CompositionState]] = field(default_factory=list, init=False, repr=False)
    _feedback_produced: list[_FeedbackMessageProvenance] = field(default_factory=list, init=False, repr=False)
    _tool_result_provenance: list[tuple[Mapping[str, Any], str, tuple[PromptGuardContentPart, ...], tuple[tuple[PromptGuardContentPart, str, PromptGuardTrust | None, str | None], ...]]] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead
        from elspeth.web.composer.service import ComposerServiceImpl
        from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
        if type(self.service) is not ComposerServiceImpl or type(self.authority) is not ComposerAsyncOperationAuthority or type(self.selected) is not SelectedContinuationState or (type(self.inventory) is not CurrentContinuationEnvInventory) or (type(self.inputs) not in (_CurrentProducerInputs, _ReadyProducerInputs)):
            raise AuditIntegrityError('message producer requires its actual current owners')
        if self.finish_read is not None:
            if type(self.finish_read) is not ContinuationFinishRead:
                raise AuditIntegrityError('message producer received a foreign owed-finish facet')
            self.finish_read.require_selection(self.selected, self.inventory)
        sessions = self.service._require_sessions_service()
        if type(sessions) is not SessionServiceImpl or sessions._engine is not self.authority._engine:
            raise AuditIntegrityError('message producer crossed actual session stores')
        states = producer_input_states(self.inputs.producer_inputs, env_ref_names=self.inventory.names)
        for entry in self.inputs.producer_inputs.schema_inputs:
            self._schema_inputs.append((_AdmittedToolCall(entry.tool_call_id, _AdmittedToolFunction('get_plugin_schema', entry.raw_arguments)), states[entry.state_index]))

    def advance_current(self, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory) -> None:
        pass

    def _require_current(self) -> _CurrentProducerInputs:
        pass

    def _require_initial_current(self) -> None:
        pass

    def _require_compose_family(self) -> None:
        pass

    def require_context(self, *, session_id: str | None, context: object, actor_user_id: str | None) -> None:
        pass

    def _register(self, message: dict[str, Any], recipe: ContinuationMessageRecipe) -> dict[str, Any]:
        pass

    def _initial(self) -> list[dict[str, Any]]:
        pass

    def initial_messages(self) -> list[dict[str, Any]]:
        pass

    def recovery_instruction(self) -> dict[str, Any]:
        pass

    def reply_instruction(self) -> dict[str, Any]:
        pass

    def tool_arguments(self, message: Mapping[str, Any]) -> dict[str, Any]:
        pass

    def register_tool_call_message(self, *, response: PrivateChunk, completion: _AdmittedLLMCompletion, message: dict[str, Any], tool_call_id: str | None=None) -> dict[str, Any]:
        pass

    def register_schema_message(self, *, call: _AdmittedToolCall, admitted: AdmittedDiscoveryResult, message: dict[str, Any]) -> dict[str, Any]:
        pass

    def snapshot_inputs(self) -> ContinuationProducerInputs:
        pass

    def register_native_message(self, *, response: PrivateChunk, completion: _AdmittedLLMCompletion, message: dict[str, Any]) -> dict[str, Any]:
        pass

    def register_failure_schema_message(self, *, response: PrivateChunk, call: _AdmittedToolCall, result: ToolResult, catalog: PolicyCatalogView, message: dict[str, Any]) -> dict[str, Any]:
        pass

    def register_attachment_message(self, *, response: PrivateChunk, call: _AdmittedToolCall, result: ToolResult, message: dict[str, Any]) -> dict[str, Any]:
        pass

    def register_tool_result_message(self, *, completion: _AdmittedLLMCompletion, tool_index: int, call: _AdmittedToolCall, result: ToolResult | AdmittedDiscoveryResult, serialized: str, message: dict[str, Any], catalog: PolicyCatalogView | None=None) -> dict[str, Any]:
        pass

    def _remember_tool_result_provenance(self, message: dict[str, Any]) -> None:
        pass

    def register_tool_feedback_message(self, *, publication: ContinuationToolMessagePublication, serialized: str, message: dict[str, Any]) -> dict[str, Any]:
        pass

    def _remember_feedback_provenance(self, *, message: dict[str, Any], recipe: ToolFeedbackMessageRecipe, call: _AdmittedToolCall, invocation: ComposerToolInvocation, authoring: PrivateToolAuthoring, outcome: _ToolOutcome | None) -> dict[str, Any]:
        pass

    def _feedback_view(self, record: _FeedbackMessageProvenance) -> OrderedObjectJSON:
        pass

    def _bind_feedback_outcome(self, publication: ContinuationToolMessagePublication, outcome: _ToolOutcome) -> None:
        pass

    def _require_tool_result_provenance(self, message: Mapping[str, Any]) -> None:
        pass

    def capture_message(self, message: Mapping[str, Any]) -> tuple[str, ContinuationMessageRecipe | None]:
        pass

    def retention_message_view(self, message: Mapping[str, Any]) -> tuple[OrderedObjectJSON, ContinuationMessageRecipe | None]:
        pass

    def _schema_message(self, index: int) -> dict[str, Any]:
        pass

    def restore_shelf(self, snapshot: MessageShelfSnapshot) -> list[dict[str, Any]]:
        pass

    def _rebuild_shelf(self, snapshot: MessageShelfSnapshot, *, inputs: _CurrentProducerInputs) -> list[dict[str, Any]]:
        pass
""",
    "failure.ContinuationToolFailureCustody": r"""@dataclass(slots=True)
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
        if self.work.running is not self.running or self.call.tool_call is not self.calls[self.call.index] or self.batch.current_call is not self.call or (self.call.audit is None):
            raise AuditIntegrityError('tool failure custody changed its actual issued owners')
        self.observation = ContinuationDispatchFailureObservation(self, self.call.audit)

    def observe_primary(self, error: BaseException) -> None:
        pass

    def observe_secondary(self, error: BaseException) -> None:
        pass

    def failure(self, error: BaseException) -> BaseException:
        pass

    def consume_nonprimary(self) -> None:
        pass

    def promote_completed_failure(self, *, kind: Literal['plugin_crash', 'advisor_failure'], original: BaseException, refusal: Literal['context_unsupported', 'provider_cost_refused', 'prompt_guard_refused'] | None=None, provider_call_id: str | None=None, allowance_limit: int | None=None, wrapper: ComposerPluginCrashError | None=None) -> None:
        pass

    def promote_completed_service_failure(self, original: _ServiceFailure) -> None:
        pass""",
    "failure.ContinuationDispatchFailureObservation": r"""@dataclass(slots=True)
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
        pass

    def require(self, recorder: BufferingRecorder, audit: DispatchAudit) -> None:
        pass

    def observe_primary(self, error: BaseException) -> None:
        pass

    def observe_execution_failure(self, error: BaseException) -> None:
        pass

    def observe_returned(self, output: object) -> None:
        pass

    def require_rebind_open(self, old: DispatchAudit) -> None:
        pass

    def observe_secondary(self, error: BaseException) -> None:
        pass

    def observe_constructed_invocation(self, invocation: ComposerToolInvocation) -> None:
        pass
""",
}
