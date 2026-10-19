"""Received diagnostics are independent of planner admission and public audit."""

import json
import warnings
from itertools import pairwise
from uuid import UUID

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.audit import BufferingRecorder, begin_dispatch, finish_plugin_crash
from elspeth.web.composer.private_diagnostic_facts import capture_failure_facts, capture_received_response
from tests.unit.web.composer.test_pipeline_planner import _pipeline, _plan, _response, _ScriptedCompletion
from tests.unit.web.composer.test_pipeline_planner_protocol_rejections import _cut_off_response


@pytest.mark.asyncio
async def test_actual_plugin_failure_and_provider_id_namespace_are_atomic_and_replayable(
    composer_service_with_real_sessions, result_session_id, monkeypatch
):
    from elspeth.web.composer import tool_batch
    from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
    from elspeth.web.composer.protocol import ComposerPluginCrashError
    from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
    from tests.unit.web.composer.test_compose_loop_persistence import _run_one_turn, _tool_batch_response

    original = tool_batch.execute_tool

    def execute(name, *args, **kwargs):
        if name == "get_pipeline_state":
            raise ValueError("ordinary missing category reference")
        return original(name, *args, **kwargs)

    async def completion(*args, **kwargs):
        return _admit_composer_llm_completion(
            _tool_batch_response(
                ("tool-failure:A", "set_metadata", {"patch": {"name": "ordinary pipeline"}}),
                ("A", "get_pipeline_state", {}),
            )
        )

    monkeypatch.setattr(tool_batch, "execute_tool", execute)
    composer = composer_service_with_real_sessions
    with pytest.raises(ComposerPluginCrashError):
        await _run_one_turn(composer, llm=completion, session_id=result_session_id)
    rows = await composer._sessions_service.get_messages(UUID(result_session_id), limit=None)
    evidence = [
        ComposerDiagnosticEvidence.from_envelope(row.tool_calls[0], expected_session_id=result_session_id)
        for row in rows
        if row.tool_calls and row.tool_calls[0].get("_kind") == "composer_diagnostic_evidence"
    ]
    assert len(evidence) == 3
    assert len({item.evidence_id for item in evidence}) == 3
    assert {item.tool_call_id for item in evidence} == {"A", "tool-failure:A"}
    failure = next(item for item in evidence if item.status == "tool_failure")
    assert "ordinary missing category reference" in failure.result.content
    public = [row for row in rows if row.role != "audit"]
    assert "ordinary missing category reference" not in json.dumps([row.content for row in public])


def test_owned_evidence_namespaces_are_bounded_for_long_provider_labels():
    from elspeth.web.composer.diagnostic_evidence import build_private_tool_evidence, diagnostic_evidence_identity

    label = "a" * 512
    evidence = build_private_tool_evidence(
        session_id="session",
        operation_id="operation",
        operation_epoch=1,
        tool_call_id=label,
        tool_name="get_pipeline_state",
        status="success",
        arguments_canonical="{}",
        result_canonical="{}",
    )
    assert len(evidence.evidence_id) < 100
    assert evidence.tool_call_id == label
    assert (
        len(
            {
                diagnostic_evidence_identity(domain, label)
                for domain in ("tool-authoring", "tool-failure", "provider-failure", "planner-candidate")
            }
        )
        == 4
    )


def test_nonnull_private_facts_preserve_exact_public_serialization_and_repr():
    from dataclasses import replace

    from elspeth.contracts.composer_audit import PrivateFailureFacts, PrivateReceivedDiagnostic
    from elspeth.web.composer.audit import audit_envelope, llm_call_audit_envelope
    from tests.unit.contracts.test_composer_audit import _make_invocation
    from tests.unit.contracts.test_composer_llm_audit import _make_call

    facts = PrivateFailureFacts('{"text":"PRIVATE FACT CANARY"}')
    receipt = PrivateReceivedDiagnostic("evidence", "call", "unknown_tool", '{"text":"PRIVATE FACT CANARY"}', "{}", "PRIVATE FACT CANARY")
    original_call, original_invocation = _make_call(), _make_invocation()
    call = replace(original_call, private_failure=facts, private_diagnostics=(receipt,))
    invocation = replace(original_invocation, private_failure=facts)
    assert call.to_dict() == original_call.to_dict()
    assert invocation.to_dict() == original_invocation.to_dict()
    assert llm_call_audit_envelope(call) == llm_call_audit_envelope(original_call)
    assert audit_envelope(invocation) == audit_envelope(original_invocation)
    assert "PRIVATE FACT CANARY" not in repr((facts, receipt, call, invocation))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["failure", "rollback", "success_in_except"])
async def test_no_recorder_owned_provider_failure_private_checkpoint_is_atomic(tmp_path, monkeypatch, mode):
    from uuid import uuid4

    import httpx
    import litellm
    from openai import APIConnectionError
    from pydantic import SecretStr

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.contracts.freeze import deep_thaw
    from elspeth.contracts.session_operation import SessionOperationKind
    from elspeth.web.composer.prompt_guard import PromptGuardMount, prompt_guard_user_scope
    from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
    from elspeth.web.composer.provider_gateway import ProviderGateway
    from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
    from elspeth.web.prompt_guard_config import PromptGuardSettings
    from elspeth.web.required_work import (
        RequiredAuthorityKind,
        RequiredWorkAuthority,
        RequiredWorkBinding,
        RequiredWorkCoordinator,
        RequiredWorkRole,
    )
    from tests.helpers.quota_guard import assert_provider_audit_companion
    from tests.unit.web.composer.test_llm_sampling_config import _settings
    from tests.unit.web.sessions.test_token_usage_adapters import _ledger, _quota_service

    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="No recorder", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    guard_requests = []

    def inspect(request):
        body = json.loads(request.content)
        guard_requests.append(body)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
            },
        )

    guard_settings = PromptGuardSettings(endpoint="https://guard.example.test", subscription_key=SecretStr("synthetic-test-key"))
    client = AzurePromptGuardClient(guard_settings, http_transport=httpx.MockTransport(inspect))
    gateway = ProviderGateway(
        model="test/model",
        settings=_settings(tmp_path, composer_prompt_guard=guard_settings),
        endpoint_base_url=None,
        endpoint_api_key=None,
    )
    monkeypatch.setattr(gateway, "_prompt_guard_mount", PromptGuardMount(client))
    original_insert = service._insert_chat_message
    original_failure = APIConnectionError(
        message="ordinary endpoint temporarily unavailable", request=httpx.Request("POST", "https://provider.invalid")
    )

    async def completion(**kwargs):
        if mode == "success_in_except":
            return litellm.ModelResponse(
                model="test/model",
                choices=[{"message": {"role": "assistant", "content": "Ordinary success.", "tool_calls": None}}],
                usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            )
        raise original_failure

    def insert(conn, **kwargs):
        if mode == "rollback" and kwargs["tool_calls"][0].get("_kind") == "composer_diagnostic_evidence":
            raise AuditIntegrityError("injected physical private insert failure")
        return original_insert(conn, **kwargs)

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(service, "_insert_chat_message", insert)
    try:

        async def invoke():
            with prompt_guard_user_scope("ordinary request"):
                await gateway._call_llm_with_audit(
                    [{"role": "user", "content": "ordinary request"}],
                    [],
                    timeout=5,
                    recorder=None,
                    provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY),
                )

        if mode == "success_in_except":
            try:
                raise ValueError("caller-private-canary")
            except ValueError:
                await invoke()
        elif mode == "rollback":
            with pytest.raises(ExceptionGroup) as caught:
                await invoke()
            assert caught.value.exceptions[0] is original_failure
            assert isinstance(caught.value.exceptions[1], AuditIntegrityError)
        else:
            with pytest.raises(APIConnectionError) as caught:
                await invoke()
            assert caught.value is original_failure
        rows = await service.get_messages(session.id, limit=None)
        assert len(guard_requests) == 1 and guard_requests[0]["userPrompt"] == "ordinary request"
        assert all(row.role == "audit" for row in rows)
        for row in rows:
            assert_provider_audit_companion(row.tool_calls, context)
        assert [row.tool_calls[0]["_kind"] for row in rows] == {
            "rollback": ["prompt_guard_audit"],
            "success_in_except": ["prompt_guard_audit", "llm_call_audit", "composer_diagnostic_evidence"],
            "failure": ["prompt_guard_audit", "llm_call_audit", "composer_diagnostic_evidence", "composer_diagnostic_evidence"],
        }[mode]
        check = rows[0].tool_calls[0]["check"]
        assert check["operation_id"] == context.fence.operation_id and check["operation_epoch"] == context.fence.operation_epoch
        assert (check["role"], check["mode"], check["adapter"], check["disposition"], check["reason"]) == (
            "composer",
            "required",
            "azure_prompt_shields",
            "allow",
            "clean",
        )
        assert check["policy_fingerprint"] == client.policy_fingerprint
        if mode == "rollback":
            assert len(rows) == 1
            assert _ledger(engine) == []
        elif mode == "success_in_except":
            assert len(rows) == 3
            assert "caller-private-canary" not in json.dumps(deep_thaw([row.tool_calls for row in rows]))
            assert len(_ledger(engine)) == 1
        else:
            assert len(rows) == 4
            assert "ordinary endpoint temporarily unavailable" in json.dumps(deep_thaw([row.tool_calls for row in rows]))
            public = next(row for row in rows if row.tool_calls[0].get("_kind") == "llm_call_audit")
            assert "ordinary endpoint temporarily unavailable" not in json.dumps(deep_thaw(public.tool_calls))
            assert len(_ledger(engine)) == 1
        if mode != "rollback":
            requests = [
                row.tool_calls[0]["evidence"]
                for row in rows
                if row.tool_calls[0].get("_kind") == "composer_diagnostic_evidence"
                and row.tool_calls[0]["evidence"]["status"] == "provider_request"
            ]
            assert len(requests) == 1
            assert json.loads(requests[0]["arguments"]["content"])["messages"] == [{"role": "user", "content": "ordinary request"}]
        coordinator.assert_completed()
    finally:
        authority.release(context)
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["length", "duplicate", "malformed"])
async def test_received_arguments_survive_pre_admission_refusal(tmp_path, tool_context, refusal):
    authored = {"pipeline": _pipeline(tmp_path)}
    if refusal == "length":
        first = _cut_off_response(completion_tokens=640, finish_reason="length")
        first.choices[0].message.tool_calls[0].function.arguments = json.dumps(authored)
    else:
        first = _response(("emit_pipeline_proposal", authored), ("list_sources", {}))
        if refusal == "duplicate":
            first.choices[0].message.tool_calls[1].id = "call-1"
        else:
            first.choices[0].message.tool_calls[0].function.arguments = '{"pipeline":'
    recorder = BufferingRecorder()
    completion = _ScriptedCompletion(first, _response(("emit_pipeline_proposal", authored)))
    if refusal == "length":
        await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion, recorder=recorder)
        assert json.loads(recorder.llm_calls[-1].private_diagnostics[0].result_canonical)["disposition"] == "admitted_receipt"
    else:
        from elspeth.web.composer.pipeline_planner import PipelinePlannerError

        with pytest.raises(PipelinePlannerError):
            await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion, recorder=recorder)
    received = recorder.llm_calls[0].private_diagnostics
    assert len(received) == (1 if refusal == "length" else 2)
    assert len({item.evidence_id for item in received}) == len(received)
    assert all(json.loads(item.result_canonical)["disposition"] == "received_not_admitted" for item in received)
    if refusal == "malformed":
        assert received[0].arguments_canonical is None
    else:
        assert json.loads(received[0].arguments_canonical) == authored


def test_benign_failure_causes_remain_distinguishable_without_public_expansion():
    audit = begin_dispatch("call-failure", "get_pipeline_state", {}, version_before=0, actor="composer-web:user:alice")
    left = finish_plugin_crash(audit, exc=ValueError("missing column category"))
    right = finish_plugin_crash(audit, exc=ValueError("missing column amount"))
    assert left.private_failure is not None
    assert right.private_failure is not None
    assert left.private_failure.canonical != right.private_failure.canonical
    assert left.error_message == right.error_message == "ValueError"
    assert "missing column" not in json.dumps(left.to_dict())


def _received(raw, *, name="set_output", call_id="call-private"):
    return {"choices": [{"message": {"tool_calls": [{"id": call_id, "function": {"name": name, "arguments": raw}}]}}]}


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '{"x":', "\ud800", '"\\ud800"', "x" * (1024 * 1024 + 1), str(2**60), None])
def test_invalid_argument_wire_is_unavailable_without_source_prefix(raw):
    (receipt,) = capture_received_response(_received(raw), namespace="request", ordinal=1, known_tools=frozenset({"set_output"}))
    assert receipt.arguments_canonical is None
    from elspeth.web.composer.diagnostic_evidence import build_private_tool_evidence

    evidence = build_private_tool_evidence(
        session_id="session",
        operation_id="operation",
        operation_epoch=1,
        tool_call_id=receipt.tool_call_id,
        tool_name=receipt.tool_name,
        evidence_id=receipt.evidence_id,
        status="received_response",
        arguments_canonical=receipt.arguments_canonical,
        raw_arguments=receipt.raw_arguments,
        result_canonical=receipt.result_canonical,
    )
    assert evidence.arguments.disposition == "unavailable"
    assert evidence.wire_arguments.disposition == "unavailable"


def test_received_work_and_namespaces_are_bounded_independently_of_admission():
    response = _received(json.dumps({"x": "a" * 600000}))
    calls = response["choices"][0]["message"]["tool_calls"]
    calls *= 100
    left = capture_received_response(response, namespace="left", ordinal=1, known_tools=frozenset())
    right = capture_received_response(response, namespace="right", ordinal=1, known_tools=frozenset())
    assert len(left) == 33
    assert sum(item.raw_arguments is not None for item in left) == 1
    assert {item.evidence_id for item in left}.isdisjoint(item.evidence_id for item in right)
    assert json.loads(left[-1].result_canonical)["omitted_slots"] == 68
    assert json.loads(left[-1].result_canonical)["shape"] == "present_calls"


def test_canonical_expansion_has_an_independent_shared_retention_budget():
    from elspeth.core.canonical import canonical_json

    assert canonical_json([1e20]) == "[100000000000000000000]"
    assert canonical_json([1]) == "[1]"
    response = _received("[" + ",".join(["1e20"] * 9000) + "]")
    response["choices"][0]["message"]["tool_calls"] *= 23
    receipts = capture_received_response(response, namespace="request", ordinal=1, known_tools=frozenset())
    assert sum(len(item.arguments_canonical.encode("utf-8")) for item in receipts if item.arguments_canonical is not None) <= 1024 * 1024
    omitted = [item for item in receipts if json.loads(item.result_canonical)["argument_disposition"] == "canonical_bounds"]
    assert omitted
    assert all(item.arguments_canonical is None and item.raw_arguments is None for item in omitted)


def test_shared_retention_budget_includes_credential_projection_expansion():
    from elspeth.web.composer.audit_storage import private_diagnostic_drafts
    from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
    from tests.unit.contracts.test_composer_llm_audit import _make_call
    from tests.unit.web.sessions.test_token_usage_adapters import _compose_context

    response = _received(json.dumps([{"token": 1}] * 2000))
    response["choices"][0]["message"]["tool_calls"] *= 32
    receipts = capture_received_response(response, namespace="r", ordinal=1, known_tools=frozenset())
    drafts = private_diagnostic_drafts(_make_call(private_diagnostics=receipts), context=_compose_context("s"))
    evidence = [ComposerDiagnosticEvidence.from_envelope(item.tool_calls[0], expected_session_id="s") for item in drafts]
    assert sum(len(item.arguments.content.encode("utf-8")) for item in evidence if item.arguments.content is not None) <= 1024 * 1024
    assert any(json.loads(item.result.content)["argument_disposition"] == "canonical_bounds" for item in evidence)


def test_failure_facts_filter_credentials_and_preserve_errno_topology():
    import errno

    failure = ValueError("password=private-canary")
    failure.__cause__ = OSError(errno.ENOENT, "ordinary missing reference")
    facts = capture_failure_facts(failure)
    value = json.loads(facts.canonical)
    assert "private-canary" not in facts.canonical
    assert value["nodes"][0]["arguments"][0]["disposition"] == "filtered"
    assert value["nodes"][1]["errno_name"] == "ENOENT"
    assert value["nodes"][1]["relation"] == "cause"
    failure.__cause__ = failure
    assert json.loads(capture_failure_facts(failure).canonical)["termination"] == "cycle"


def test_failure_graph_retains_cause_and_suppressed_context():
    failure = RuntimeError("outer operation")
    failure.__cause__ = ValueError("primary cause")
    failure.__context__ = LookupError("suppressed context")
    failure.__suppress_context__ = True
    facts = json.loads(capture_failure_facts(failure).canonical)
    assert [node["class"] for node in facts["nodes"]] == ["RuntimeError", "ValueError", "LookupError"]
    assert facts["nodes"][0]["suppressed_context"] is True
    assert facts["edges"] == [
        {"from": 0, "to": 1, "relation": "cause", "disposition": "captured"},
        {"from": 0, "to": 2, "relation": "context", "disposition": "captured"},
    ]
    assert facts["termination"] == "complete"


def test_failure_graph_distinguishes_group_shared_nodes_from_cycles():
    shared = ValueError("shared leaf")
    branch = RuntimeError("branch")
    group = ExceptionGroup("group", [branch, shared, shared])
    branch.__cause__ = group
    facts = json.loads(capture_failure_facts(group).canonical)
    assert [node["class"] for node in facts["nodes"]] == ["ExceptionGroup", "RuntimeError", "ValueError"]
    assert [(edge["relation"], edge["from"], edge["to"], edge["disposition"]) for edge in facts["edges"]] == [
        ("group_member", 0, 1, "captured"),
        ("cause", 1, 0, "cycle"),
        ("group_member", 0, 2, "captured"),
        ("group_member", 0, 2, "shared"),
    ]
    assert [edge["member_index"] for edge in facts["edges"] if edge["relation"] == "group_member"] == [0, 1, 2]
    assert facts["termination"] == "cycle"
    shared_only = json.loads(capture_failure_facts(ExceptionGroup("shared", [shared, shared])).canonical)
    assert shared_only["termination"] == "complete"
    independent = ValueError("shared leaf")
    assert json.loads(capture_failure_facts(ExceptionGroup("shared", [independent, independent])).canonical) == shared_only


@pytest.mark.parametrize("message", ["\x01" * 2048, "a" * 2048, "😀" * 512], ids=["escaped", "ascii", "nonbmp"])
def test_failure_graph_survives_argument_serialization_bounds(message):
    failure = ValueError(message, message, message, "password=graph-private-canary")
    failure.__cause__ = LookupError(message, message, message, "essential root cause")
    captured = capture_failure_facts(failure)
    facts = json.loads(captured.canonical)
    assert len(captured.canonical.encode("utf-8")) <= 8192
    assert facts["record_type"] == "failure_facts"
    assert [node["class"] for node in facts["nodes"]] == ["ValueError", "LookupError"]
    assert facts["edges"][0]["to"] == 1
    assert facts["nodes"][0]["arguments"][3]["disposition"] == "filtered"
    assert "graph-private-canary" not in captured.canonical
    assert facts["trimmed_arguments"] > 0
    assert sum(slot.get("text_omitted", False) for node in facts["nodes"] for slot in node["arguments"]) == facts["trimmed_arguments"]
    assert capture_failure_facts(failure).canonical == captured.canonical


def test_failure_graph_caps_depth_nodes_members_and_edges_explicitly():
    chain = [ValueError(f"level {index}") for index in range(9)]
    for parent, child in pairwise(chain):
        parent.__cause__ = child
    deep = json.loads(capture_failure_facts(chain[0]).canonical)
    assert len(deep["nodes"]) == 5
    assert deep["edges"][-1]["to"] is None
    assert deep["edges"][-1]["disposition"] == "depth_limit"
    assert deep["termination"] == "node_limit"
    assert "depth_limit" in deep["termination_reasons"]

    members = [ValueError(f"member {index}") for index in range(12)]
    wide = json.loads(capture_failure_facts(ExceptionGroup("wide", members)).canonical)
    assert len(wide["nodes"]) == 8
    assert wide["nodes"][0]["omitted_members"] == 4
    assert len(wide["edges"]) == 8
    assert wide["edges"][-1]["disposition"] == "node_limit"
    assert set(wide["termination_reasons"]) == {"node_limit", "group_member_limit"}

    dense = ExceptionGroup("dense", [members[0]] * 8)
    members[0].__cause__ = members[0]
    members[0].__context__ = ExceptionGroup("nested", [members[0]] * 8)
    bounded = json.loads(capture_failure_facts(dense).canonical)
    assert len(bounded["edges"]) == 16
    assert bounded["omitted_edges"] > 0
    assert "edge_limit" in bounded["termination_reasons"]
    assert sum(sum(node.get("omitted_relations", {}).values()) for node in bounded["nodes"]) == bounded["omitted_edges"]


def test_failure_graph_existing_targets_survive_depth_and_node_caps():
    chain = [ValueError(f"level {index}") for index in range(5)]
    for parent, child in pairwise(chain):
        parent.__cause__ = child
    chain[-1].__cause__ = chain[0]
    chain[0].__context__ = chain[-1]
    facts = json.loads(capture_failure_facts(chain[0]).canonical)
    assert facts["edges"][-2]["disposition"] == "cycle"
    assert facts["edges"][-1]["disposition"] == "shared"
    assert "depth_limit" not in facts["termination_reasons"]

    members = [ValueError(f"member {index}") for index in range(7)]
    capped = json.loads(capture_failure_facts(ExceptionGroup("full", [*members, members[-1]])).canonical)
    assert len(capped["nodes"]) == 8
    assert capped["edges"][-1]["disposition"] == "shared"
    assert capped["termination"] == "complete"


def test_failure_graph_bounds_escaped_classes_and_maximal_disposition_skeleton():
    import errno

    # Legal class labels can expand in canonical JSON independently of UTF8.
    label = "\x01" * 20 + "a" * 36
    failure_type = type(label, (OSError,), {})
    group_type = type(label, (ExceptionGroup,), {})
    leaves = [failure_type(errno.ENOENT, "ordinary reference") for _ in range(7)]
    group = group_type("group", leaves)
    for leaf in leaves:
        BaseException.__dict__["args"].__set__(leaf, ("\x01" * 2048,) * 4)
        leaf.__cause__ = group
        leaf.__context__ = group
    BaseException.__dict__["args"].__set__(group, ("\x01" * 2048,) * 4)
    captured = capture_failure_facts(group)
    facts = json.loads(captured.canonical)
    assert len(captured.canonical.encode("utf-8")) <= 8192
    assert facts["nodes"]
    assert all(node["class"] == label for node in facts["nodes"])
    assert facts["trimmed_arguments"] == 4 * len(facts["nodes"])
    assert all(slot["text_omitted"] is True for node in facts["nodes"] for slot in node["arguments"])
    assert all(node["errno_name"] == "ENOENT" for node in facts["nodes"][1:])
    unbounded_label = type("\x01" * 128, (ValueError,), {})
    assert json.loads(capture_failure_facts(unbounded_label()).canonical)["nodes"][0]["class"] == "unavailable"


def test_failure_graph_base_exception_members_and_snapshot_are_private_and_immutable():
    import asyncio

    failure = ValueError("ordinary private reference")
    group = BaseExceptionGroup("joined outcomes", [failure, asyncio.CancelledError()])
    captured = capture_failure_facts(group)
    before = captured.canonical
    failure.__cause__ = RuntimeError("later replacement")
    facts = json.loads(before)
    assert [node["class"] for node in facts["nodes"]] == ["BaseExceptionGroup", "ValueError", "CancelledError"]
    assert captured.canonical == before
    assert capture_failure_facts(group).canonical != before
    assert "ordinary private reference" not in repr(captured)
    assert "later replacement" not in before


def test_failure_graph_uses_builtin_storage_for_hostile_group_and_exception():
    class HostileGroup(ExceptionGroup):
        @property
        def exceptions(self):
            raise AssertionError("group property executed")

        @property
        def __class__(self):
            raise AssertionError("class property executed")

        def __str__(self):
            raise AssertionError("str executed")

        def __repr__(self):
            raise AssertionError("repr executed")

    class HostileFailure(Exception):
        def __getattribute__(self, name):
            raise AssertionError("exception attribute executed")

    leaf = HostileFailure("ordinary private detail")
    group = HostileGroup("group", [leaf])
    facts = json.loads(capture_failure_facts(group).canonical)
    assert [node["class"] for node in facts["nodes"]] == ["HostileGroup", "HostileFailure"]
    assert facts["edges"][0]["relation"] == "group_member"


@pytest.mark.parametrize("message", ["\ud800", "a" * 2049])
def test_failure_malformed_or_overbound_text_is_unavailable(message):
    facts = json.loads(capture_failure_facts(ValueError(message)).canonical)
    assert facts["nodes"][0]["arguments"] == [{"disposition": "unavailable", "text": None}]


def test_capture_never_calls_hostile_conversion_or_descriptor():
    class Hostile:
        @property
        def __dict__(self):
            raise AssertionError("descriptor executed")

        def __str__(self):
            raise AssertionError("str executed")

        def __repr__(self):
            raise AssertionError("repr executed")

    summary = capture_received_response(Hostile(), namespace="request", ordinal=1, known_tools=frozenset())
    assert json.loads(summary[0].result_canonical)["shape"] == "unavailable_envelope"
    assert json.loads(capture_failure_facts(ValueError(Hostile())).canonical)["nodes"][0]["arguments"][0]["text"] is None


def test_aliased_builtin_descriptor_and_hostile_exception_class_are_not_executed():
    class Alias:
        __dict__ = type.__dict__["__dict__"]

    class Failure(Exception):
        @property
        def __class__(self):
            raise AssertionError("class callback executed")

    assert (
        json.loads(capture_received_response(Alias(), namespace="request", ordinal=1, known_tools=frozenset())[0].result_canonical)["shape"]
        == "unavailable_envelope"
    )
    assert json.loads(capture_failure_facts(Failure("ordinary missing reference")).canonical)["nodes"][0]["class"] == "Failure"


def test_received_dictionary_key_callbacks_and_overwide_envelopes_are_unavailable():
    class Key:
        def __hash__(self):
            return hash("choices")

        def __eq__(self, other):
            raise AssertionError("key equality executed")

    for response in ({Key(): "hostile"}, {str(index): index for index in range(65)}):
        facts = capture_received_response(response, namespace="r", ordinal=1, known_tools=frozenset())
        assert json.loads(facts[0].result_canonical)["shape"] == "unavailable_envelope"


def test_received_foreign_type_metaclass_callbacks_are_not_invoked():
    class HostileMeta(type):
        def __hash__(cls):
            raise AssertionError("metaclass hash executed")

        def __eq__(cls, other):
            raise AssertionError("metaclass equality executed")

    class Opaque(metaclass=HostileMeta):
        pass

    for response in ({"choices": Opaque()}, {"choices": [{"message": {"tool_calls": Opaque()}}]}):
        facts = capture_received_response(response, namespace="r", ordinal=1, known_tools=frozenset())
        assert json.loads(facts[0].result_canonical)["shape"] in {"unavailable_envelope", "unavailable_calls"}


def test_foreign_class_dictionary_key_callback_is_not_invoked():
    armed = False

    class Key:
        def __hash__(self):
            return hash("__dict__")

        def __eq__(self, other):
            if armed:
                raise AssertionError("class dictionary key equality executed")
            return False

    key = Key()
    # Python versions differ in whether class creation warns about this key.
    # The capture contract is that the retained hostile key is never executed.
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always", RuntimeWarning)
        foreign = type("Foreign", (), {key: 1})
    assert any(item is key for item in vars(foreign))
    armed = True
    with pytest.raises(AssertionError, match="class dictionary key equality executed"):
        assert key == "__dict__"
    facts = capture_received_response(foreign(), namespace="r", ordinal=1, known_tools=frozenset())
    assert json.loads(facts[0].result_canonical)["shape"] == "unavailable_envelope"


@pytest.mark.asyncio
async def test_long_terminal_provider_id_does_not_become_a_diagnostic_refusal(tmp_path, tool_context):
    from elspeth.web.composer.diagnostic_evidence import build_private_tool_evidence

    response = _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)}))
    response.choices[0].message.tool_calls[0].id = "a" * 1000
    recorder = BufferingRecorder()
    await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=_ScriptedCompletion(response), recorder=recorder)
    candidate = recorder.private_candidates[0]
    assert candidate.status == "accepted"
    evidence = build_private_tool_evidence(
        session_id="session",
        operation_id="operation",
        operation_epoch=1,
        tool_call_id=candidate.tool_call_id,
        evidence_id=candidate.evidence_id,
        tool_name=candidate.tool_name,
        status=candidate.status,
        arguments_canonical=candidate.arguments_canonical,
        result_canonical=candidate.result_canonical,
        authority_arguments_canonical=candidate.authority_arguments_canonical,
        raw_arguments=candidate.raw_arguments,
    )
    assert len(evidence.tool_call_id) < 100


def test_empty_response_and_unavailable_response_have_distinct_closed_dispositions():
    empty = capture_received_response({"choices": [{"message": {"tool_calls": []}}]}, namespace="r", ordinal=1, known_tools=frozenset())
    unavailable = capture_received_response({}, namespace="r", ordinal=1, known_tools=frozenset())
    assert json.loads(empty[0].result_canonical)["shape"] == "empty_calls"
    assert json.loads(unavailable[0].result_canonical)["shape"] == "unavailable_envelope"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["length", "duplicate", "malformed", "unsent_known", "credential_metadata", "provider_failure", "metadata_secondary"]
)
async def test_owned_physical_receipt_reaches_private_sql_read_without_reaccounting(tmp_path, test_client, monkeypatch, mode):
    from dataclasses import replace
    from uuid import uuid4

    import httpx
    import litellm
    from openai import APIConnectionError

    from elspeth.contracts.freeze import deep_thaw
    from elspeth.contracts.session_operation import SessionOperationKind
    from elspeth.web.catalog.policy_view import PolicyCatalogView
    from elspeth.web.composer.capability_skill import load_pipeline_capability_core
    from elspeth.web.composer.pipeline_planner import PipelinePlannerError, PlannerOriginatingMessage, plan_pipeline
    from elspeth.web.composer.pipeline_proposal import AbsentBase
    from elspeth.web.composer.provider_quota import ProviderInvocationOwner
    from elspeth.web.credential_guard import CredentialMaterialRefused
    from elspeth.web.dependencies import create_catalog_service
    from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
    from elspeth.web.required_work import (
        RequiredAuthorityKind,
        RequiredWorkAuthority,
        RequiredWorkBinding,
        RequiredWorkCoordinator,
        RequiredWorkRole,
    )
    from elspeth.web.sessions.protocol import CompositionStateData
    from elspeth.web.sessions.routes._helpers import _persist_turn_audit_cohort
    from tests.unit.web.composer.test_pipeline_planner import _budget, _custody, _empty_state, _lifecycle, _model
    from tests.unit.web.composer.test_pipeline_planner_protocol_rejections import _length_stopped_tool_response
    from tests.unit.web.sessions.test_record_audit_grade_view import _get
    from tests.unit.web.sessions.test_token_usage_adapters import _ledger, _quota_service

    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="Received private", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    response = (
        _length_stopped_tool_response(tmp_path)
        if mode == "length"
        else _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)}), ("list_sources", {}))
    )
    if mode == "duplicate":
        response.choices[0].message.tool_calls[1].id = "call-1"
    if mode == "credential_metadata":
        response.model = "password=metadata-private-canary"
    if mode == "malformed":
        response.choices[0].message.tool_calls[0].function.arguments = '{"pipeline":'
    if mode == "unsent_known":
        response = _response(
            ("set_output", {"output_id": "out", "plugin": "database", "options": {"url": "postgresql://opaque-private-location"}})
        )

    async def completion(**kwargs):
        if mode == "provider_failure":
            raise APIConnectionError(message="ordinary reference unavailable", request=httpx.Request("POST", "https://provider.invalid"))
        return response

    monkeypatch.setattr(litellm, "acompletion", completion)
    primary_metadata_failure = ValueError("ordinary metadata construction failure")
    secondary_audit_failure = AuditIntegrityError("controlled secondary evidence failure")
    if mode == "metadata_secondary":
        from elspeth.web.composer import pipeline_planner

        def fail_metadata(**kwargs):
            if "response" in kwargs:
                raise primary_metadata_failure
            raise secondary_audit_failure

        monkeypatch.setattr(pipeline_planner, "build_llm_call_record", fail_metadata)
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    recorder = BufferingRecorder()
    try:
        state = await service.save_composition_state(
            session.id, CompositionStateData(is_valid=True), provenance="session_seed", session_operation_context=context
        )
        with pytest.raises((PipelinePlannerError, CredentialMaterialRefused, APIConnectionError, ExceptionGroup)) as caught:
            await plan_pipeline(
                intent="Build requested pipeline",
                current_state=_empty_state(),
                provider_current_state=_empty_state().to_dict(),
                schemas_loaded=frozenset(),
                mark_schema_loaded=None,
                policy_catalog=PolicyCatalogView.for_trained_operator(catalog, snapshot),
                plugin_snapshot=snapshot,
                originating_message=PlannerOriginatingMessage(
                    session_id=str(session.id), message_id=str(uuid4()), content="Build requested pipeline", user_id="alice"
                ),
                base=AbsentBase(),
                model_config=_model(_ScriptedCompletion(), max_api_attempts=1),
                rendered_skill=load_pipeline_capability_core(),
                repair_budget=1,
                budget_policy=_budget(),
                custody_config=replace(
                    _custody(tmp_path), session_engine=engine, session_operation_context=context, session_operation_authority=authority
                ),
                lifecycle=_lifecycle(),
                recorder=recorder,
                candidate_finalizer=lambda value: value,
                provider_service=service,
                provider_owner=owner,
            )
        if mode == "metadata_secondary":

            def leaves(failure):
                if isinstance(failure, BaseExceptionGroup):
                    return [leaf for member in failure.exceptions for leaf in leaves(member)]
                return [failure]

            assert primary_metadata_failure in leaves(caught.value)
            assert secondary_audit_failure in leaves(caught.value)
            return
        before = len(_ledger(engine))
        assert before == len(recorder.llm_calls) > 0
        physical_rows = await service.get_messages(session.id, limit=None)
        assert any(row.tool_calls and row.tool_calls[0].get("_kind") == "composer_diagnostic_evidence" for row in physical_rows)
        for _ in range(2):
            await _persist_turn_audit_cohort(
                service,
                session.id,
                (),
                recorder.llm_calls,
                tool_composition_state_id=str(state.id),
                llm_composition_state_id=str(state.id),
                plugin_crash_pending=False,
                session_operation_context=context,
            )
        assert len(_ledger(engine)) == before
        all_rows = await service.get_messages(session.id, limit=None)
        physical_private = [row for row in all_rows if row.tool_calls and row.tool_calls[0].get("_kind") == "composer_diagnostic_evidence"]
        assert all(row.composition_state_id is None for row in physical_private)
        monkeypatch.setattr(test_client.app.state, "session_service", service)
        monkeypatch.setattr(test_client.app.state, "phase3_engine", engine)
        private = await _get(test_client, f"/api/sessions/{session.id}/diagnostic-evidence?limit=5")
        assert private.status_code == 200
        payload = private.json()
        assert payload
        encoded = json.dumps(payload)
        assert "metadata-private-canary" not in encoded
        assert "opaque-private-location" not in encoded
        if mode == "provider_failure":
            assert "ordinary reference unavailable" in encoded
        else:
            assert "received_tool_arguments" in encoded
            assert recorder.llm_calls[0].call_id in encoded
        public = await _get(test_client, f"/api/sessions/{session.id}/messages")
        assert public.status_code == 200
        assert "received_tool_arguments" not in json.dumps(public.json())
        assert "ordinary reference unavailable" not in json.dumps(public.json())
        for call in recorder.llm_calls:
            assert "private_diagnostics" not in call.to_dict()
            assert "private_failure" not in call.to_dict()
        assert "metadata-private-canary" not in json.dumps(deep_thaw([call.to_dict() for call in recorder.llm_calls]))
    finally:
        authority.release(context)
        engine.dispose()
