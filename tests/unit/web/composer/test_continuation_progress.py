import json
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.advisor_output import (
    ContinuationAdvisorDecision,
    ReadContinuationAction,
    parse_continuation_advisor_decision,
)
from elspeth.web.composer.continuation_progress import (
    ActionExecutionReceipt,
    ContinuationFrontier,
    ContinuationObligation,
    ContinuationProtocolError,
    continuation_action_key,
    require_bound_continuation_decision,
)


def stop_decision():
    return {
        "schema_version": "composer.continuation-decision.v1",
        "decision": "STOP_CONFIRMED",
        "scope": "whole_request",
        "obligation_ids": ["request"],
        "obligations": [],
        "evidence": [],
        "reason": "A reviewed blocker covers the request.",
        "remaining_actionable_ids": [],
        "prescribed_action": None,
    }


def test_continue_without_action_is_not_a_continuation():
    data = stop_decision()
    assert parse_continuation_advisor_decision(json.dumps(data), actual_advisor_call_id="actual-call:1") is not None
    data["decision"] = "CONTINUE"
    assert parse_continuation_advisor_decision(json.dumps(data), actual_advisor_call_id="actual-call:1") is None


def test_whole_stop_cannot_hide_actionable_remainder_or_user_question():
    data = stop_decision()
    data["remaining_actionable_ids"] = ["request/child"]
    assert parse_continuation_advisor_decision(json.dumps(data), actual_advisor_call_id="actual-call:1") is None
    data["remaining_actionable_ids"] = []
    data["question"] = "Continue?"
    assert parse_continuation_advisor_decision(json.dumps(data), actual_advisor_call_id="actual-call:1") is None


def test_only_the_actual_owner_assigns_the_physical_decision_identity():
    data = stop_decision()
    decision = parse_continuation_advisor_decision(json.dumps(data), actual_advisor_call_id="actual-call:1")
    assert decision is not None and decision.decision_id == "actual-call:1"
    data["decision_id"] = "model-invented-physical-id"
    assert parse_continuation_advisor_decision(json.dumps(data), actual_advisor_call_id="actual-call:1") is None


def test_same_action_cannot_gain_freshness_from_rollover_or_advice_words():
    root = ContinuationObligation("request", None, "whole request", "original", 0, 13)
    frontier = ContinuationFrontier((root,), (), (), (), "g")
    action = ReadContinuationAction(
        kind="read_tool",
        obligation_ids=("request",),
        evidence_ids=(),
        input_hash="i",
        graph_hash="g",
        policy_hash="p",
        tool_manifest_hash="t",
        tool_name="get_plugin_schema",
        arguments_json='{"name":"csv","type":"source"}',
    )
    decision = ContinuationAdvisorDecision(
        schema_version="composer.continuation-decision.v1",
        decision_id="advisor:1",
        decision="CONTINUE",
        scope="whole_request",
        obligation_ids=("request",),
        obligations=(),
        evidence=(),
        reason="Inspect the missing contract.",
        remaining_actionable_ids=("request",),
        prescribed_action=action,
    )
    kwargs = {
        "actual_advisor_call_id": "advisor:1",
        "retained_evidence": {},
        "input_hash": "i",
        "policy_hash": "p",
        "tool_manifest_hash": "t",
        "permitted_read_tools": frozenset({"get_plugin_schema"}),
        "complete_required_coverage": True,
    }
    require_bound_continuation_decision(decision, frontier=frontier, **kwargs)
    seen = replace(
        frontier,
        action_receipts=(
            ActionExecutionReceipt(
                continuation_action_key(action),
                "tool-receipt:1",
                "committed",
            ),
        ),
    )
    with pytest.raises(ContinuationProtocolError, match="already been prepared"):
        require_bound_continuation_decision(decision, frontier=seen, **kwargs)
    with pytest.raises(ContinuationProtocolError, match="mutation as a read"):
        require_bound_continuation_decision(decision, frontier=frontier, **{**kwargs, "permitted_read_tools": frozenset()})


def test_completed_action_history_does_not_permanently_block_whole_stop():
    from elspeth.web.composer.advisor_output import ContinuationObligationProposal
    from elspeth.web.composer.continuation_progress import record_action_receipt

    root = ContinuationObligation("request", None, "whole request", "original", 0, 13)
    frontier = ContinuationFrontier((root,), (), (), (), "g")
    for disposition in ("prepared", "dispatched", "committed"):
        frontier = record_action_receipt(
            frontier,
            ActionExecutionReceipt("action:1", "effect:1", disposition),
            actual_owner_receipt_id="effect:1",
        )
    stop = ContinuationAdvisorDecision(
        schema_version="composer.continuation-decision.v1",
        decision_id="advisor:stop",
        decision="STOP_CONFIRMED",
        scope="whole_request",
        obligation_ids=("request",),
        obligations=(
            ContinuationObligationProposal(
                obligation_id="request",
                parent_id=None,
                description="Reviewed whole-request blocker",
                original_message_id="original",
                start=0,
                end=13,
                status="blocked",
            ),
        ),
        evidence=(),
        reason="The current reviewed blocker covers the whole request.",
        remaining_actionable_ids=(),
        prescribed_action=None,
    )
    kwargs = {
        "actual_advisor_call_id": "advisor:stop",
        "retained_evidence": {},
        "input_hash": "i",
        "policy_hash": "p",
        "tool_manifest_hash": "t",
        "permitted_read_tools": frozenset(),
        "complete_required_coverage": True,
    }
    require_bound_continuation_decision(stop, frontier=frontier, **kwargs)
    with pytest.raises(ContinuationProtocolError, match="unresolved action custody"):
        require_bound_continuation_decision(
            stop,
            frontier=replace(frontier, action_receipts=frontier.action_receipts[:2]),
            **kwargs,
        )
    with pytest.raises(AuditIntegrityError):
        record_action_receipt(
            frontier,
            ActionExecutionReceipt("action:1", "effect:1", "refused"),
            actual_owner_receipt_id="effect:1",
        )
    with pytest.raises(AuditIntegrityError):
        record_action_receipt(
            frontier,
            ActionExecutionReceipt("action:1", "different-effect", "committed"),
            actual_owner_receipt_id="different-effect",
        )


def test_subset_stop_cannot_drop_still_open_whole_request():
    root = ContinuationObligation("request", None, "whole request", "original", 0, 13)
    decision = ContinuationAdvisorDecision(
        schema_version="composer.continuation-decision.v1",
        decision_id="advisor:subset",
        decision="STOP_CONFIRMED",
        scope="subset",
        obligation_ids=("request",),
        obligations=(),
        evidence=(),
        reason="One item is blocked.",
        remaining_actionable_ids=(),
        prescribed_action=None,
    )
    with pytest.raises(ContinuationProtocolError, match="omitted an open obligation"):
        require_bound_continuation_decision(
            decision,
            actual_advisor_call_id="advisor:subset",
            frontier=ContinuationFrontier((root,), (), (), (), "g"),
            retained_evidence={},
            input_hash="i",
            policy_hash="p",
            tool_manifest_hash="t",
            permitted_read_tools=frozenset(),
            complete_required_coverage=True,
        )


def test_new_obligation_labels_do_not_change_the_host_source_identity():
    from elspeth.web.composer.advisor_output import ContinuationObligationProposal
    from elspeth.web.composer.continuation_progress import canonicalize_continuation_obligations

    root = ContinuationObligation("request", None, "whole request", "original", 0, 13)
    frontier = ContinuationFrontier((root,), (), (), (), "g")

    def proposed(label, description):
        action = ReadContinuationAction(
            kind="read_tool",
            obligation_ids=(label,),
            evidence_ids=(),
            input_hash="i",
            graph_hash="g",
            policy_hash="p",
            tool_manifest_hash="t",
            tool_name="get_plugin_schema",
            arguments_json='{"name":"csv","type":"source"}',
        )
        return ContinuationAdvisorDecision(
            schema_version="composer.continuation-decision.v1",
            decision_id="actual-call",
            decision="CONTINUE",
            scope="subset",
            obligation_ids=(label,),
            obligations=(
                ContinuationObligationProposal(
                    obligation_id=label,
                    parent_id="request",
                    description=description,
                    original_message_id="original",
                    start=0,
                    end=5,
                    status="open",
                ),
            ),
            evidence=(),
            reason="Inspect this source-bound requirement.",
            remaining_actionable_ids=("request", label),
            prescribed_action=action,
        )

    first = canonicalize_continuation_obligations(proposed("first-label", "Inspect the input"), frontier)
    renamed = canonicalize_continuation_obligations(proposed("new-label", "Read the input"), frontier)
    assert first.obligations[0].obligation_id == renamed.obligations[0].obligation_id
    assert first.prescribed_action is not None and renamed.prescribed_action is not None
    assert continuation_action_key(first.prescribed_action) == continuation_action_key(renamed.prescribed_action)
    assert canonicalize_continuation_obligations(first, frontier) == first


def test_duplicate_physical_evidence_cannot_create_a_new_action_premise():
    from elspeth.web.composer.advisor_output import ContinuationEvidenceRef

    first = ContinuationEvidenceRef(
        receipt_id="physical-one",
        content_hash="f" * 64,
        original_attempt_id="attempt-one",
        coverage_binding="same-complete-tool-input-and-range",
    )
    duplicate = first.model_copy(update={"receipt_id": "physical-two", "original_attempt_id": "attempt-two"})
    action = ReadContinuationAction(
        kind="read_tool",
        obligation_ids=("request",),
        evidence_ids=(first.receipt_id,),
        input_hash="i",
        graph_hash="g",
        policy_hash="p",
        tool_manifest_hash="t",
        tool_name="get_plugin_schema",
        arguments_json='{"name":"csv","type":"source"}',
    )
    repeated = action.model_copy(update={"evidence_ids": (duplicate.receipt_id,)})
    assert continuation_action_key(action, retained_evidence={first.receipt_id: first}) == continuation_action_key(
        repeated,
        retained_evidence={duplicate.receipt_id: duplicate},
    )


@pytest.mark.parametrize("encoded", ['{"decision":1,"decision":2}', "NaN", "[]", '{"future":true}'])
def test_decision_parser_rejects_non_owned_or_non_strict_json(encoded):
    assert parse_continuation_advisor_decision(encoded, actual_advisor_call_id="actual") is None


def test_parser_requires_actual_advisor_owner():
    with pytest.raises(AuditIntegrityError, match="actual Advisor call"):
        parse_continuation_advisor_decision(json.dumps(stop_decision()), actual_advisor_call_id="")


def _frontier_and_decision():
    root = ContinuationObligation("request", None, "whole request", "original", 0, 13)
    action = ReadContinuationAction(
        kind="read_tool",
        obligation_ids=("request",),
        evidence_ids=(),
        input_hash="i",
        graph_hash="g",
        policy_hash="p",
        tool_manifest_hash="t",
        tool_name="get_plugin_schema",
        arguments_json='{"name":"csv"}',
    )
    decision = ContinuationAdvisorDecision(
        schema_version="composer.continuation-decision.v1",
        decision_id="advisor:1",
        decision="CONTINUE",
        scope="whole_request",
        obligation_ids=("request",),
        obligations=(),
        evidence=(),
        reason="Read its contract.",
        remaining_actionable_ids=("request",),
        prescribed_action=action,
    )
    return ContinuationFrontier((root,), (), (), (), "g"), decision


def _require(decision, frontier, **updates):
    kwargs = {
        "actual_advisor_call_id": "advisor:1",
        "retained_evidence": {},
        "input_hash": "i",
        "policy_hash": "p",
        "tool_manifest_hash": "t",
        "permitted_read_tools": frozenset({"get_plugin_schema"}),
        "complete_required_coverage": True,
    }
    require_bound_continuation_decision(decision, frontier=frontier, **{**kwargs, **updates})


@pytest.mark.parametrize(
    "field,value",
    [
        ("input_hash", "other"),
        ("graph_hash", "other"),
        ("policy_hash", "other"),
        ("tool_manifest_hash", "other"),
        ("obligation_ids", ("missing",)),
        ("evidence_ids", ("missing",)),
    ],
)
def test_action_requires_current_owned_premises(field, value):
    frontier, decision = _frontier_and_decision()
    _require(decision, frontier)
    changed = decision.model_copy(update={"prescribed_action": decision.prescribed_action.model_copy(update={field: value})})
    with pytest.raises(ContinuationProtocolError):
        _require(changed, frontier)


def test_obligation_updates_keep_original_source_and_reject_duplicate_spans():
    from elspeth.web.composer.advisor_output import ContinuationObligationProposal
    from elspeth.web.composer.continuation_progress import canonicalize_continuation_obligations, proposed_obligations

    frontier, decision = _frontier_and_decision()
    child = ContinuationObligationProposal(
        obligation_id="child", parent_id="request", description="Read", original_message_id="original", start=0, end=5, status="open"
    )
    accepted = decision.model_copy(update={"obligations": (child,)})
    assert len(proposed_obligations(accepted, frontier)) == 2
    for changed in (child.model_copy(update={"original_message_id": "other"}), child.model_copy(update={"end": 14})):
        with pytest.raises(ContinuationProtocolError, match="original human source"):
            proposed_obligations(decision.model_copy(update={"obligations": (changed,)}), frontier)
    with pytest.raises(ContinuationProtocolError, match="same immutable source span"):
        canonicalize_continuation_obligations(
            decision.model_copy(update={"obligations": (child, child.model_copy(update={"obligation_id": "alias"}))}), frontier
        )


def test_action_receipt_custody_is_once_only_and_monotone():
    from elspeth.web.composer.continuation_progress import record_action_receipt

    frontier, _ = _frontier_and_decision()
    prepared = ActionExecutionReceipt("key", "owner", "prepared")
    with pytest.raises(AuditIntegrityError):
        record_action_receipt(frontier, prepared, actual_owner_receipt_id="impostor")
    frontier = record_action_receipt(frontier, prepared, actual_owner_receipt_id="owner")
    unknown = ActionExecutionReceipt("key", "owner", "unknown")
    frontier = record_action_receipt(frontier, unknown, actual_owner_receipt_id="owner")
    with pytest.raises(AuditIntegrityError, match="moved backwards"):
        record_action_receipt(frontier, prepared, actual_owner_receipt_id="owner")
    committed = ActionExecutionReceipt("key", "owner", "committed")
    frontier = record_action_receipt(frontier, committed, actual_owner_receipt_id="owner")
    assert record_action_receipt(frontier, committed, actual_owner_receipt_id="owner") is frontier


def test_frontier_chunk_roundtrip_retains_actual_bytes_and_nominal_owner():
    from elspeth.web.composer.continuation_codec import encode_frontier_chunk, verify_private_chunk
    from elspeth.web.composer.continuation_progress import decode_continuation_frontier, encode_continuation_frontier

    frontier, _ = _frontier_and_decision()
    chunk = encode_frontier_chunk(frontier)
    assert chunk.reference.codec == "composer.semantic-frontier.v1"
    assert chunk.canonical == encode_continuation_frontier(frontier)
    verify_private_chunk(chunk.reference, chunk.canonical)
    assert decode_continuation_frontier(chunk.canonical) == frontier
    with pytest.raises(TypeError, match="owned product value"):
        encode_frontier_chunk(object())


def test_retained_action_requires_exact_advisor_source_and_selected_frontier():
    from elspeth.web.composer.continuation import ContinuationActionSource, PrivateChunkRef
    from elspeth.web.composer.continuation_codec import encode_frontier_chunk
    from elspeth.web.composer.continuation_progress import retain_advisor_action
    from tests.unit.web.composer.test_continuation_frames import initial_stack

    frontier, decision = _frontier_and_decision()
    chunk = encode_frontier_chunk(frontier)
    stack = initial_stack().model_copy(update={"semantic_frontier": chunk.reference})
    source = ContinuationActionSource(
        effect_key="provider:1:0",
        input_sha256="a" * 64,
        response=PrivateChunkRef(codec="composer.response.v1", digest="b" * 64, byte_length=2),
    )
    kwargs = {
        "actual_advisor_call_id": "advisor:1",
        "frontier": frontier,
        "actual_source": source,
        "retained_evidence": {},
        "input_hash": "i",
        "policy_hash": "p",
        "tool_manifest_hash": "t",
        "permitted_read_tools": frozenset({"get_plugin_schema"}),
        "complete_required_coverage": True,
    }
    retained, actual_chunk = retain_advisor_action(stack, decision, **kwargs)
    assert actual_chunk.reference == chunk.reference
    assert retained.active_action_source is source
    assert json.loads(retained.active_action_json)["kind"] == "read_tool"
    assert stack.active_action_json is None
    with pytest.raises(ContinuationProtocolError, match="still owns execution"):
        retain_advisor_action(retained, decision, **kwargs)
    with pytest.raises(AuditIntegrityError, match="selected complete snapshot"):
        retain_advisor_action(initial_stack(), decision, **kwargs)
    with pytest.raises(TypeError, match="exact current SQL-verified source"):
        retain_advisor_action(stack, decision, **{**kwargs, "actual_source": object()})


def test_provider_receipts_and_json_key_order_cannot_create_action_novelty():
    from elspeth.web.composer.advisor_output import ContinuationEvidenceRef

    _, decision = _frontier_and_decision()
    action = decision.prescribed_action
    first = ContinuationEvidenceRef(
        receipt_id="provider-one", source_kind="provider", content_hash="a" * 64, original_attempt_id="attempt-one", coverage_binding="one"
    )
    second = first.model_copy(update={"receipt_id": "provider-two", "content_hash": "b" * 64})
    assert continuation_action_key(
        action.model_copy(update={"evidence_ids": (first.receipt_id,)}), retained_evidence={first.receipt_id: first}
    ) == continuation_action_key(
        action.model_copy(update={"evidence_ids": (second.receipt_id,)}), retained_evidence={second.receipt_id: second}
    )
    left = action.model_copy(update={"arguments_json": '{"a":1,"b":2}'})
    right = action.model_copy(update={"arguments_json": '{"b":2,"a":1}'})
    assert continuation_action_key(left) == continuation_action_key(right)
    with pytest.raises(ContinuationProtocolError, match="one strict object"):
        continuation_action_key(action.model_copy(update={"arguments_json": '{"a":1,"a":2}'}))


def test_frontier_and_action_codecs_refuse_unknown_fields_and_duplicate_keys():
    from elspeth.web.composer.continuation_progress import (
        decode_continuation_frontier,
        decode_prescribed_continuation_action,
        encode_continuation_frontier,
    )

    frontier, decision = _frontier_and_decision()
    encoded = encode_continuation_frontier(frontier)
    assert decode_continuation_frontier(encoded) == frontier
    data = json.loads(encoded)
    data["future"] = True
    with pytest.raises(AuditIntegrityError, match="owned codec"):
        decode_continuation_frontier(json.dumps(data))
    with pytest.raises(AuditIntegrityError, match="strict JSON"):
        decode_continuation_frontier('{"graph_hash":"a","graph_hash":"b"}')
    action_json = decision.prescribed_action.model_dump_json()
    assert decode_prescribed_continuation_action(action_json) == decision.prescribed_action
    with pytest.raises(AuditIntegrityError, match="strict JSON"):
        decode_prescribed_continuation_action('{"kind":"read_tool","kind":"review"}')


def test_response_schema_has_closed_action_discriminator_without_physical_identity():
    from elspeth.web.composer.advisor_output import continuation_advisor_response_format

    schema = continuation_advisor_response_format()
    assert schema["json_schema"]["strict"] is True
    body = schema["json_schema"]["schema"]
    assert body["additionalProperties"] is False
    assert "decision_id" not in body["properties"]
    assert "prescribed_action" in body["required"]


def test_action_multiplicity_cannot_bypass_committed_semantic_recurrence():
    frontier, decision = _frontier_and_decision()
    action = decision.prescribed_action
    assert action is not None
    # Bind the exact unique action before recording its actual committed owner.
    assert continuation_action_key(action) == "3447c1805cecf995b650fa6ca75a5eae35469b7b8ac003be04405585b6f4c5a4"
    _require(decision, frontier)
    seen = replace(frontier, action_receipts=(ActionExecutionReceipt(continuation_action_key(action), "owner", "committed"),))
    with pytest.raises(ContinuationProtocolError, match="already been prepared"):
        _require(decision, seen)
    duplicate = action.model_copy(update={"obligation_ids": ("request", "request")})
    with pytest.raises(ContinuationProtocolError, match="repeated an action obligation"):
        _require(decision.model_copy(update={"prescribed_action": duplicate}), seen)
    changed = action.model_copy(update={"arguments_json": '{"name":"json"}'})
    _require(decision.model_copy(update={"prescribed_action": changed}), seen)


@pytest.mark.parametrize("admission", ["parser", "cold"])
def test_public_advisor_parser_and_cold_action_decoder_reject_duplicate_action_ids(admission):
    from elspeth.web.composer.continuation_progress import decode_prescribed_continuation_action

    _, decision = _frontier_and_decision()
    payload = decision.model_dump(mode="json", exclude={"decision_id"})
    encoded = json.dumps(payload)
    assert parse_continuation_advisor_decision(encoded, actual_advisor_call_id="advisor:1") is not None
    action_json = json.dumps(payload["prescribed_action"])
    assert decode_prescribed_continuation_action(action_json) == decision.prescribed_action
    payload["prescribed_action"]["obligation_ids"] = ["request", "request"]
    if admission == "parser":
        assert parse_continuation_advisor_decision(json.dumps(payload), actual_advisor_call_id="advisor:1") is None
    else:
        with pytest.raises(AuditIntegrityError, match="owned codec"):
            decode_prescribed_continuation_action(json.dumps(payload["prescribed_action"]))


def test_resolved_action_aliases_and_order_cannot_create_freshness():
    from elspeth.web.composer.advisor_output import ContinuationObligationProposal
    from elspeth.web.composer.continuation_progress import canonicalize_continuation_obligations

    frontier, base = _frontier_and_decision()
    assert base.prescribed_action is not None

    def proposed(label, order):
        proposal = ContinuationObligationProposal(
            obligation_id=label,
            parent_id="request",
            description="Inspect input",
            original_message_id="original",
            start=0,
            end=5,
            status="open",
        )
        action = base.prescribed_action.model_copy(update={"obligation_ids": order})
        return base.model_copy(
            update={
                "obligation_ids": (label,),
                "remaining_actionable_ids": ("request", label),
                "obligations": (proposal,),
                "prescribed_action": action,
            }
        )

    first = proposed("first-label", ("request", "first-label"))
    canonical = canonicalize_continuation_obligations(first, frontier)
    assert canonical.prescribed_action is not None
    key = continuation_action_key(canonical.prescribed_action)
    seen = replace(frontier, action_receipts=(ActionExecutionReceipt(key, "owner", "committed"),))
    _require(first, frontier)
    renamed_and_reordered = proposed("second-label", ("second-label", "request"))
    normalized = canonicalize_continuation_obligations(renamed_and_reordered, frontier)
    assert normalized.prescribed_action is not None
    assert continuation_action_key(normalized.prescribed_action) == key
    with pytest.raises(ContinuationProtocolError, match="already been prepared"):
        _require(renamed_and_reordered, seen)
    canonical_child = canonical.obligations[0].obligation_id
    # Distinct raw IDs resolve to the same original span and must be rejected.
    aliased_duplicate = proposed("another-label", ("another-label", canonical_child))
    with pytest.raises(ContinuationProtocolError, match="repeated an action obligation"):
        _require(aliased_duplicate, seen)
    changed_action = renamed_and_reordered.prescribed_action.model_copy(update={"arguments_json": '{"name":"json"}'})
    _require(renamed_and_reordered.model_copy(update={"prescribed_action": changed_action}), seen)


def _parsed_span_decision(frontier, proposals, selected, *, arguments='{"name":"csv","type":"source"}', existing=False):
    from elspeth.web.composer.advisor_output import ContinuationObligationProposal

    offered = tuple(
        ContinuationObligationProposal(
            obligation_id=label,
            parent_id=parent,
            description="Inspect input",
            original_message_id="original",
            start=start,
            end=end,
            status="open",
        )
        for label, parent, start, end in proposals
    )
    action = ReadContinuationAction(
        kind="read_tool",
        obligation_ids=(selected,),
        evidence_ids=(),
        input_hash="i",
        graph_hash="g",
        policy_hash="p",
        tool_manifest_hash="t",
        tool_name="get_plugin_schema",
        arguments_json=arguments,
    )
    decision = ContinuationAdvisorDecision(
        schema_version="composer.continuation-decision.v1",
        decision_id="advisor:1",
        decision="CONTINUE",
        scope="subset",
        obligation_ids=(selected,),
        obligations=offered,
        evidence=(),
        reason="Inspect source-bound input.",
        remaining_actionable_ids=tuple(item.obligation_id for item in frontier.obligations if item.status == "open")
        + (() if existing else tuple(item.obligation_id for item in offered)),
        prescribed_action=action,
    )
    parsed = parse_continuation_advisor_decision(decision.model_dump_json(exclude={"decision_id"}), actual_advisor_call_id="advisor:1")
    assert parsed == decision
    return parsed


def test_equal_span_descendant_cannot_repeat_committed_parsed_read_after_cold_roundtrip():
    from elspeth.web.composer.continuation_progress import (
        apply_bound_continuation_decision,
        canonicalize_continuation_obligations,
        decode_continuation_frontier,
        encode_continuation_frontier,
        initial_continuation_frontier,
        record_action_receipt,
    )

    frontier = initial_continuation_frontier(request="whole request", original_message_id="original", graph_hash="g")
    kwargs = {
        "actual_advisor_call_id": "advisor:1",
        "retained_evidence": {},
        "input_hash": "i",
        "policy_hash": "p",
        "tool_manifest_hash": "t",
        "permitted_read_tools": frozenset({"get_plugin_schema"}),
        "complete_required_coverage": True,
    }
    first = _parsed_span_decision(frontier, (("first", "request", 0, 5),), "first")
    canonical = canonicalize_continuation_obligations(first, frontier)
    assert canonical.prescribed_action is not None
    original_key = continuation_action_key(canonical.prescribed_action)
    assert original_key == "0bea0a8026c671742436ec1ae1cf712588c670eb31af6951d52f9205343e8744"
    parent = canonical.obligations[0].obligation_id
    frontier = apply_bound_continuation_decision(first, frontier=frontier, **kwargs)
    receipt = ActionExecutionReceipt(original_key, "tool:original", "committed")
    frontier = record_action_receipt(frontier, receipt, actual_owner_receipt_id="tool:original")
    frontier = decode_continuation_frontier(encode_continuation_frontier(frontier))
    assert frontier.action_receipts == (receipt,)
    alias = _parsed_span_decision(frontier, (("alias", "request", 0, 5),), "alias", existing=True)
    with pytest.raises(ContinuationProtocolError, match="already been prepared"):
        _require(alias, frontier)
    changed = _parsed_span_decision(
        frontier, (("changed", "request", 0, 5),), "changed", existing=True, arguments='{"name":"json","type":"source"}'
    )
    _require(changed, frontier)
    narrower = _parsed_span_decision(frontier, (("narrower", parent, 1, 4),), "narrower")
    _require(narrower, frontier)
    repeated = _parsed_span_decision(frontier, (("nested", parent, 0, 5),), "nested")
    with pytest.raises(ContinuationProtocolError, match="duplicate immutable source binding"):
        apply_bound_continuation_decision(repeated, frontier=frontier, **kwargs)


@pytest.mark.parametrize("shape", ["same_decision", "cold_retained", "root_span"])
def test_complete_merged_frontier_refuses_duplicate_source_spans(shape):
    from elspeth.web.composer.continuation_progress import (
        canonicalize_continuation_obligations,
        decode_continuation_frontier,
        encode_continuation_frontier,
        initial_continuation_frontier,
    )

    frontier = initial_continuation_frontier(request="whole request", original_message_id="original", graph_hash="g")
    if shape == "root_span":
        decision = _parsed_span_decision(frontier, (("duplicate-root", "request", 0, 13),), "duplicate-root")
    else:
        decision = _parsed_span_decision(frontier, (("child", "request", 0, 5), ("nested", "child", 0, 5)), "nested")
        if shape == "cold_retained":
            canonical = canonicalize_continuation_obligations(decision, frontier)
            # Model previously retained owned values through the real cold codec.
            # No new proposal exists when the reducer must detect their overlap.
            retained = tuple(
                ContinuationObligation(
                    item.obligation_id, item.parent_id, item.description, item.original_message_id, item.start, item.end, item.status
                )
                for item in canonical.obligations
            )
            frontier = decode_continuation_frontier(
                encode_continuation_frontier(replace(frontier, obligations=(*frontier.obligations, *retained)))
            )
            assert frontier.obligations[-2].start == frontier.obligations[-1].start
            assert frontier.obligations[-2].end == frontier.obligations[-1].end
            decision = _parsed_span_decision(
                frontier, (), retained[-1].obligation_id, existing=True, arguments='{"name":"json","type":"source"}'
            )
            assert decision.obligations == ()
    with pytest.raises(ContinuationProtocolError, match="duplicate immutable source binding"):
        _require(decision, frontier)
