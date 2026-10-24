"""Complete executable contracts for the bounded B2 Advisor source path.

These are syntax contracts, not hashes or executable substitutes. Compare ASTs
with locations/comments excluded; re-review intentional source changes.
"""

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
    from elspeth.web.composer.provider_thinking import attach_provider_thinking

    llm_messages.append(
        attach_provider_thinking(
            {
                "role": "assistant",
                "content": assistant_message.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in assistant_tool_calls
                ],
            },
            completion.provider_metadata.thinking_blocks,
        )
    )

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
    batch.phase = "call_ready" if assistant_tool_calls else "dispatch_ready"
''',
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
            _retain_llm_tool_argument_parts(llm_messages, tool_call_id=tool_call.id)
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
    return "stop"
''',
    "batch._execute_continuation_call_live": r'''async def _execute_continuation_call_live(
    *,
    call_model: _CallModelOutcome,
    ctx: ToolBatchContext,
    acc: BatchAccumulator,
    llm_messages: list[dict[str, Any]],
    batch: ContinuationBatchState,
    call: ContinuationCallState,
) -> Literal["applied"]:
    """Execute the original suffix with the actual caller's revocation owner."""
    recorder = ctx.recorder
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
                    llm_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": cached_json,
                            PROMPT_GUARD_CONTENT_PARTS_KEY: tool_result_content_parts(
                                cached_admitted if cached_admitted is not None else cached_result, cached_json
                            ),
                        }
                    )
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
                        audit = rebind_dispatch_arguments(audit, audit_arguments)
                        decoded_args_by_call_id[tool_call.id] = audit_arguments
                        transcript_arguments = (
                            redact_tool_call_arguments(tool_name, arguments, telemetry=ctx.redaction_telemetry)
                            if candidate.prepared_inline_blob is not None
                            else audit_arguments
                        )
                        _replace_llm_tool_call_arguments(
                            llm_messages,
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
                                audit = rebind_dispatch_arguments(audit, audit_arguments)
                                decoded_args_by_call_id[tool_call.id] = audit_arguments
                                redacted_arguments = redact_tool_call_arguments(
                                    tool_name,
                                    arguments,
                                    telemetry=ctx.redaction_telemetry,
                                )
                                _replace_llm_tool_call_arguments(
                                    llm_messages,
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
                                audit = rebind_dispatch_arguments(audit, arguments)
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

            await _await_started_tool_stage(emit_progress(progress, tool_started_progress_event(tool_name)), audit=audit, recorder=recorder)

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
                    llm_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_feedback_json,
                            PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                        }
                    )
                    turn_has_discovery = True
                    continue

                budget = ctx.advisor_max_calls_per_compose
                if advisor_calls_used >= budget:
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
                    llm_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_feedback_json,
                            PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                        }
                    )
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
                    llm_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_feedback_json,
                            PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                        }
                    )
                    turn_has_discovery = True
                    continue

                advisor_timeout = ctx.advisor_timeout_seconds
                effective_advisor_timeout = min(advisor_timeout, remaining)
                advisor_deadline_limited = remaining <= advisor_timeout

                # F2: consume budget BEFORE the outbound call, not after.
                # The cost guard's purpose is to bound outbound LiteLLM
                # calls regardless of outcome. Counting only successes
                # would let a flaky provider rack up unlimited failed
                # outbound calls until anti-anchor or the discovery-
                # turn limit fired. ARG_ERROR (above) and pre-call compose
                # timeout (above) do NOT consume advisor budget because no
                # outbound advisor call is made.
                advisor_calls_used += 1

                abandonment_context = None
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
                        ),
                        audit=audit,
                        recorder=recorder,
                    )
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
                    llm_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_feedback_json,
                            PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                        }
                    )
                    turn_has_discovery = True
                    continue

                except Exception as advisor_context_error:
                    advisor_outcome = _AdvisorFirstPartyFailure(original_exc=advisor_context_error)

                if type(advisor_outcome) is _AdvisorProviderFailure:
                    # The service owns the Tier-3 provider taxonomy and returns
                    # this explicit recoverable outcome. Budget was already
                    # consumed above: the outbound attempt counts whether or not
                    # it produced guidance.
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
                    llm_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_feedback_json,
                            PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                        }
                    )
                    turn_has_discovery = True
                    continue

                if type(advisor_outcome) is _AdvisorFirstPartyFailure:
                    # A controlled-code fault is not provider feedback. Close the
                    # outer dispatch truthfully, carry it through P4, and let the
                    # driver re-raise this exact exception object after the tool
                    # row has been published.
                    first_party_exc = advisor_outcome.original_exc
                    if isinstance(first_party_exc, CommonProviderCostRefused):
                        refusal_payload = {
                            "success": False,
                            "status": "PROVIDER_POLICY_REFUSED",
                            "code": first_party_exc.code,
                            "outbound_call_made": first_party_exc.call_id is not None,
                        }
                        recorder.record(finish_success(audit, result_payload=refusal_payload, version_after=state.version))
                        _append_tool_outcome(
                            response=refusal_payload,
                            error_class=None,
                            error_category=None,
                            error_message=None,
                            post_version=state.version,
                        )
                        advisor_failure = first_party_exc
                        break
                    if isinstance(first_party_exc, PromptGuardRefused):
                        # The tool completed with a semantic refusal. It is neither
                        # provider-outage feedback nor a plugin implementation fault.
                        refusal_payload = {"success": False, "outbound_call_made": False, **first_party_exc.to_payload()}
                        recorder.record(finish_success(audit, result_payload=refusal_payload, version_after=state.version))
                        _append_tool_outcome(
                            response=refusal_payload,
                            error_class=None,
                            error_category=None,
                            error_message=None,
                            post_version=state.version,
                        )
                        advisor_failure = first_party_exc
                        break
                    recorder.record(finish_plugin_crash(audit, exc=first_party_exc))
                    _append_tool_outcome(
                        response=None,
                        error_class=type(first_party_exc).__name__,
                        error_category=None,
                        error_message=type(first_party_exc).__name__,
                        post_version=state.version,
                    )
                    advisor_failure = first_party_exc
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
                        _pending_review_runtime_findings(state, ctx), audit=audit, recorder=recorder
                    )
                except ComposerRuntimePreflightError as preflight_exc:
                    raise ComposerRuntimePreflightError(
                        original_exc=preflight_exc.original_exc,
                        partial_state=preflight_exc.partial_state,
                        tool_invocations=recorder.invocations,
                        llm_calls=recorder.llm_calls,
                        failed_turn=preflight_exc.failed_turn,
                    ) from preflight_exc.original_exc
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
                        ),
                        audit=audit,
                        recorder=recorder,
                    )
                except (AssertionError, MemoryError, RecursionError, SystemError):
                    # Same narrow-class discipline as the sync execute_tool
                    # handler below: interpreter/Tier-1 invariant states must
                    # unwind, never be laundered into a recoverable envelope.
                    raise
                except AuditIntegrityError:
                    # Tier-1 audit invariant — same passthrough as the sync path.
                    raise
                except Exception as tool_exc:
                    # elspeth-9c01c943a5: the session-aware dispatch converts
                    # ToolArgumentError internally, so any exception reaching
                    # here is a plugin/service bug. Without this capture it
                    # escaped run_tool_batch and surfaced as an uncoded 500
                    # with the accumulated turn state silently dropped. Route
                    # it through the same plugin-crash carrier as the sync
                    # path so the turn persists and the route layer answers
                    # with the coded envelope.
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
                    break
                all_cache_hits = False
                _append_tool_outcome(
                    response=session_aware_outcome.result,
                    error_class=session_aware_outcome.error_class,
                    error_category=session_aware_outcome.error_category,
                    error_message=session_aware_outcome.error_message,
                    post_version=session_aware_outcome.post_version,
                )
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
                    recorder.record(finish_cancelled(audit, exc=cancellation))
                    raise
                except ComposerRuntimePreflightError as preflight_exc:
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
                        )
                    except ComposerRuntimePreflightError as preflight_exc:
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
            ) -> Any:
                if _preproposal_exception is not None:
                    raise _preproposal_exception
                if _prevalidated_unapplied_result is not None:
                    return _prevalidated_unapplied_result
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
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_feedback_json,
                        PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                    }
                )
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

            llm_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result_json,
                    PROMPT_GUARD_CONTENT_PARTS_KEY: tool_result_content_parts(
                        admitted_result if admitted_result is not None else result, result_json
                    ),
                }
            )

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
    return "applied"
''',
    "batch._finish_continuation_batch_live": r'''def _finish_continuation_batch_live(
    *,
    call_model: _CallModelOutcome,
    ctx: ToolBatchContext,
    acc: BatchAccumulator,
    batch: ContinuationBatchState,
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
        advisor_compose_timeout=advisor_compose_timeout,
        assistant_message=assistant_message,
        raw_assistant_content=raw_assistant_content,
        assistant_tool_calls=assistant_tool_calls,
        mutation_success_observed=mutation_success_observed,
        pre_state_id=pre_state_id,
        authority_revocation=acc.authority_revocation,
    )
    return dispatch, advisor_calls_used
''',
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
""",
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
    "batch._retain_llm_tool_argument_parts": r'''def _retain_llm_tool_argument_parts(llm_messages: Sequence[dict[str, Any]], *, tool_call_id: str) -> None:
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
    "batch._await_started_tool_stage": r'''async def _await_started_tool_stage[T](awaitable: Awaitable[T], *, audit: DispatchAudit, recorder: BufferingRecorder) -> T:
    """Close a started dispatch when one unaudited async stage cancels."""
    try:
        return await awaitable
    except asyncio.CancelledError as cancellation:
        recorder.record(finish_cancelled(audit, exc=cancellation))
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
    "stamp_planner_terminal",
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


# Built once, at import, in registry order (``wire_secret_ref`` last).
_WIRE_TOOL_DEFS: Final[Mapping[ToolContractDialect, Mapping[str, WireTool]]] = build_wire_tool_defs(
    get_tool_definitions(),
    limits=OPENAI_STRICT_LIMITS,
)

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


def stamp_planner_terminal(definition: Mapping[str, Any], dialect: ToolContractDialect) -> dict[str, Any]:
    """Return a copy of the planner terminal tool stamped for ``dialect``.

    The terminal is never strict-capable in S1: ``openai_strict`` gets an
    explicit ``strict: false``; ``none`` gets an unchanged copy.
    """
    stamped = deepcopy(dict(definition))
    if dialect == ToolContractDialect.OPENAI_STRICT:
        stamped["function"]["strict"] = False
    return stamped


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
