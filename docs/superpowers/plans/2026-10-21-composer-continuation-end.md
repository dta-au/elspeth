# Composer Continuation END and Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resume the actual no-tool, recovery and continuation-control owners through current publication review, scoped semantic assessment and truthful terminal settlement.

**Architecture:** Retain the complete candidate and each actual reviewed source as private closed values. Reuse current source-proof, interpretation, preflight and finalization owners; keep actual publication admission separate from semantic whole-request stopping. Every SQL application selects one complete checkpoint, verifies actual physical receipts and commits its audit/caller transition together.

**Tech Stack:** Python 3.13, existing Composer/Advisor and graph-recovery dataclasses, strict Pydantic private codecs, SQLAlchemy current-owner transactions, ordinary pytest and real-worker cold-cut fixtures.

Production source is pinned to `b003e0cd9c6c6a7b0968635a9c03f71a83bc2e73`. The later `2e869240a388d1ede0454f1fbb9b019d22a9a9da` qualification is test-only and does not implement these steps. The plan's actual physical audit/outcome closure is a separate required dependency; a ledger alone never recreates a missing provider audit or claims an SDK was not called.

The coordinator has since qualified that exact test-only checkpoint locally and created the isolated integration target. Its later actual PromptShield caller commit is `9f321eb8da5b5f2214caf163561875626b247954`; neither qualification implements the END packet below. Reconcile its required current `content_scope` argument at F13/T1 as already specified by the frames/producers companions. Preserve the exact existing typed request-rejection and current-scope refusal paths.

## E0: Freeze the exact END entry and current semantic bindings

**Create:** `src/elspeth/web/composer/continuation_semantic_bindings.py` and `src/elspeth/web/composer/continuation_end.py`.
**Modify:** `sessions/continuation.py`'s action receipt check and the concrete frames dispatcher.

- [ ] Add this complete current binding producer. Its tools come from the same actual current v2 palette as F14. Its policy hash includes current owned plugin, guard and secret-wiring policy identities, never the timing-policy hash or an API key. It does not grant execution authority: normal current session/resource/guard validation still runs at every actual use.

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.prompt_guard import PromptGuardMount
from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
from elspeth.web.composer.tools._registry import continuation_declaration
from elspeth.web.composer.tools.declarations import ToolKind

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime
    from elspeth.web.composer.service import ComposerServiceImpl


def current_continuation_semantic_bindings(
    service: ComposerServiceImpl, runtime: RestoredContinuationRuntime,
) -> tuple[str, str, frozenset[str]]:
    from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime
    from elspeth.web.composer.service import ComposerServiceImpl
    if type(service) is not ComposerServiceImpl or type(runtime) is not RestoredContinuationRuntime:
        raise TypeError("semantic policy requires the actual current service/runtime owners")
    current_tools = composer_loop_tool_definitions(service._planner_dialect, include_continuation=True)
    if current_tools != runtime.live.tools:
        raise AuditIntegrityError("semantic action saw a stale current provider tool palette")
    guards: dict[str, str | None] = {}
    for role, mount in (("primary", service._provider_gateway.prompt_guard_mount),
                        ("advisor", service._advisor_prompt_guard_mount)):
        if mount is not None and type(mount) is not PromptGuardMount:
            raise AuditIntegrityError("semantic policy lacks an owned current guard mount")
        guards[role] = None if mount is None else mount.client.policy_fingerprint
    policy = stable_hash({
        "schema": "composer.semantic-policy.v1",
        "plugin_snapshot_hash": runtime.bindings.plugin_snapshot.snapshot_hash,
        "plugin_policy_hash": runtime.bindings.plugin_snapshot.policy_hash,
        "guard_policies": guards, "guard_mode": service._settings.composer_prompt_guard_mode,
        "secret_wiring_hash": service._secret_wiring_policy.canonical_hash,
        "tool_dialect": service._planner_dialect.value,
    })
    names: set[str] = set()
    for tool in current_tools:
        name = tool["function"]["name"]
        declaration = continuation_declaration(name)
        if declaration is None or name == "preview_pipeline":
            continue
        if declaration.kind in (ToolKind.DISCOVERY, ToolKind.SECRET_DISCOVERY):
            names.add(name)
        elif name in ("list_blobs", "list_composer_blobs", "get_blob_metadata", "get_blob_content", "read_attachment", "inspect_source"):
            names.add(name)
    return policy, stable_hash({"schema": "composer.tool-manifest.v1", "tools": current_tools}), frozenset(names)
```

- [ ] In storage `retain_action_execution_receipt_on_connection`, replace only `if action.policy_hash != stack.policy_hash or stack.semantic_frontier is None:` with the exact condition below. `stack.policy_hash` hashes the complete retained timing/admission policy. The action's independently bound semantic security policy is checked by the current producer above before dispatch and by E4 at semantic acceptance; the immutable actual effect retains it in `action_json`. A late actual settlement remains attributable after current policy changes.

```python
if stack.semantic_frontier is None:
    raise AuditIntegrityError("prepared action lost its retained semantic frontier")
```

- [ ] Add this exact outward contract to `continuation_end.py`; the executable body in E2–E5 uses the concrete F13–F22 and C3/C5 owners. The frames dispatcher routes Compose `response_retained` no-tool/control/recovery, an existing `EndFrame`, and its automatic Advisor child here. It never treats `result=None` as a durable yield receipt.

```python-signature
async def advance_continuation_end(
    *, service: ComposerServiceImpl, operation_authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState, required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner, progress: ComposerProgressSink | None,
) -> SelectedContinuationState | ContinuationAdvance:
```

## E1: Retain the complete candidate and actual reviewed sources

**Create:** `src/elspeth/web/composer/continuation_end_values.py`.
**Test:** `tests/unit/web/composer/test_continuation_end.py`.

- [ ] Add this complete pure packet. Candidate text is exact provider-authored text, not synthesized graph structure. The original response remains referenced so controls and native content are independently recoverable. A publication/semantic result names its exact received source, not the latest Advisor call.

```python
from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import ContinuationActionSource, ContinuationValue, PrivateChunkRef
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_private_chunk, verify_private_chunk


class ContinuationControlHandoff(ContinuationValue):
    predecessor_sequence: int = Field(gt=0)
    predecessor_generation: int = Field(gt=0)
    successor_sequence: int = Field(gt=0)
    successor_generation: int = Field(gt=0)

    @model_validator(mode="after")
    def require_one_actual_successor(self) -> Self:
        if (self.successor_sequence, self.successor_generation) != (
                self.predecessor_sequence + 1, self.predecessor_generation + 1):
            raise ValueError("control handoff changed its exact queued successor")
        return self


class EndContinuationCandidate(ContinuationValue):
    schema_version: Literal["composer.end.v1"] = "composer.end.v1"
    kind: Literal["reply", "recovery", "control"]
    response: PrivateChunkRef
    primary_effect_key: str = Field(pattern=r"^provider:0:(0|[1-9][0-9]*)$")
    primary_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_text: str
    state: PrivateChunkRef
    evidence: PrivateChunkRef
    iteration: PrivateChunkRef
    superseded_reply_index: int | None = Field(default=None, ge=0)
    runtime_preflight_json: str | None = None
    preparation_feedback: tuple[str, ...] = ()
    allow_repair_continue: bool = True
    publication_binding: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    publication_source: ContinuationActionSource | None = None
    publication_record_json: str | None = None
    semantic_source: ContinuationActionSource | None = None
    semantic_decision_json: str | None = None
    control_handoff: ContinuationControlHandoff | None = None

    @model_validator(mode="after")
    def require_complete_actual_sources(self) -> Self:
        expected = ((self.response, "composer.response.v1"), (self.state, "composer.state.v1"),
                    (self.evidence, "composer.evidence.v1"), (self.iteration, "composer.iteration.v1"))
        if any(ref.codec != codec for ref, codec in expected):
            raise ValueError("END candidate lost a complete actual source variant")
        if (self.publication_source is None) != (self.publication_record_json is None):
            raise ValueError("END publication lost its actual pass source/record pair")
        if (self.semantic_source is None) != (self.semantic_decision_json is None):
            raise ValueError("END semantic assessment lost its actual source/decision pair")
        if self.control_handoff is not None and self.kind != "control":
            raise ValueError("only the scheduling control can retain a handoff receipt")
        return self


def encode_end_candidate(value: EndContinuationCandidate) -> PrivateChunk:
    if type(value) is not EndContinuationCandidate:
        raise TypeError("END candidate encoding requires its complete owned value")
    return encode_private_chunk(codec="composer.end.v1", canonical=canonical_json(value.model_dump(mode="json")))


def decode_end_candidate(chunk: PrivateChunk) -> EndContinuationCandidate:
    if chunk.reference.codec != "composer.end.v1":
        raise AuditIntegrityError("END frame selected another private codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    try:
        result = EndContinuationCandidate.model_validate_json(chunk.canonical, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("END candidate violates its complete closed shape") from error
    if canonical_json(result.model_dump(mode="json")) != chunk.canonical:
        raise AuditIntegrityError("END candidate lost or changed fields on decode")
    return result


def continuation_candidate_id(candidate: EndContinuationCandidate) -> str:
    return "candidate:" + stable_hash({"text": candidate.candidate_text})
```

- [ ] Replace storage's connection-owning `load_private_chunks` with this complete closed nested walk. Its pure `required_private_chunks(stack)` prepass stays unchanged. It protects the exact End candidate's response/evidence/iteration and the actual Advisor source refs after parent owner capture changes their current refs. Each nested chunk is read under the same session/operation and digest-verified; no reflection or latest-row search is used.

```python
def load_private_chunks(
    conn: Connection, *, session_id: str, operation_id: str, stack: ContinuationFrameStack,
) -> tuple[PrivateChunk, ...]:
    from elspeth.web.composer.continuation_advisor import decode_continuation_advisor_invocation
    from elspeth.web.composer.continuation_end_values import decode_end_candidate
    pending = list(required_private_chunks(stack))
    loaded: dict[tuple[str, str], PrivateChunk] = {}
    while pending:
        ref = pending.pop(0)
        identity = ref.codec, ref.digest
        previous = loaded.get(identity)
        if previous is not None:
            if previous.reference != ref:
                raise AuditIntegrityError("nested private reference changed its exact retained byte bound")
            continue
        canonical = read_chunk(conn, session_id=session_id, operation_id=operation_id,
            key=f"value:{ref.codec}:{ref.digest}").decode("utf-8", errors="strict")
        verify_private_chunk(ref, canonical)
        actual = PrivateChunk(ref, canonical)
        loaded[identity] = actual
        if ref.codec == "composer.end.v1":
            candidate = decode_end_candidate(actual)
            pending.extend((candidate.response, candidate.state, candidate.evidence, candidate.iteration))
            if candidate.publication_source is not None:
                pending.append(candidate.publication_source.response)
            if candidate.semantic_source is not None:
                pending.append(candidate.semantic_source.response)
        elif ref.codec == "composer.advisor.v1":
            source = decode_continuation_advisor_invocation(actual)
            pending.extend((source.state, source.evidence, source.transcript, source.iteration))
            if source.premise is not None:
                pending.append(source.premise.frontier)
    return tuple(loaded.values())
```

E1's pure candidate and F13's pure Advisor codec precede this loader packet. Their runtime END/Advisor dispatch bodies are not imported here. Normal owner model/chunk imports stay in the existing SQL module.

- [ ] Add these complete pure tests. Bootstrap the importable packet before the behavioral RED. Temporarily drop `candidate_text` from encoder input and require the equality/content assertion to fail, then restore it; missing imports or collection are setup errors.

```python
import pytest

from elspeth.web.composer.continuation import ContinuationActionSource, PrivateChunkRef
from elspeth.web.composer.continuation_end_values import EndContinuationCandidate, decode_end_candidate, encode_end_candidate


def end_ref(codec, digit):
    return PrivateChunkRef(codec=codec, digest=digit * 64, byte_length=1)


def test_end_candidate_roundtrip_keeps_exact_response_sources_and_words():
    actual = EndContinuationCandidate(kind="reply", response=end_ref("composer.response.v1", "a"),
        primary_effect_key="provider:0:0", primary_input_sha256="0" * 64,
        candidate_text="Keep café and the exact final λ claim.", state=end_ref("composer.state.v1", "b"),
        evidence=end_ref("composer.evidence.v1", "c"), iteration=end_ref("composer.iteration.v1", "d"))
    restored = decode_end_candidate(encode_end_candidate(actual))
    assert restored == actual and restored.candidate_text == "Keep café and the exact final λ claim."
    assert restored.response == actual.response and restored.semantic_source is None


def test_end_candidate_requires_received_source_and_decision_together():
    actual = EndContinuationCandidate(kind="reply", response=end_ref("composer.response.v1", "a"),
        primary_effect_key="provider:0:0", primary_input_sha256="0" * 64,
        candidate_text="Exact candidate", state=end_ref("composer.state.v1", "b"),
        evidence=end_ref("composer.evidence.v1", "c"), iteration=end_ref("composer.iteration.v1", "d"))
    source = ContinuationActionSource(effect_key="provider:1:0", input_sha256="e" * 64,
        response=end_ref("composer.response.v1", "f"))
    with pytest.raises(ValueError):
        EndContinuationCandidate.model_validate({**actual.model_dump(), "semantic_source": source})
    with pytest.raises(ValueError):
        EndContinuationCandidate.model_validate({**actual.model_dump(), "publication_record_json": "{}"})
```

## E2: Prepare the actual no-tool candidate without running an END provider inline

**Modify:** `composer/continuation_end.py`, `coordination/composer_operation_authority.py`; extend `sessions/continuation.py`.

- [ ] Add these complete imports and result-projection helpers to `continuation_end.py`. They project only the actual registered SQL outcome; they do not accept a business callback or mint an authority object. Each SQL call below names its concrete owner directly. All deferred cancellation objects are delivered after the corresponding complete SQL result is projected.

```python
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.continuation import AdvisorFrame, ComposeFrame, EndFrame
from elspeth.web.composer.continuation_advisor import (
    ContinuationAdvisorInvocation, ContinuationAdvisorPremise,
    decode_continuation_advisor_invocation, encode_continuation_advisor_invocation,
    nearest_compose_frame,
)
from elspeth.web.composer.continuation_codec import (
    PrivateChunk, decode_continuation_response, decode_continuation_snapshot,
    decode_frontier_chunk, encode_continuation_snapshot, encode_frontier_chunk,
)
from elspeth.web.composer.continuation_end_values import (
    EndContinuationCandidate, continuation_candidate_id, decode_end_candidate, encode_end_candidate,
)
from elspeth.web.composer.continuation_execution import (
    SelectedContinuationState, commit_continuation_phase, continuation_chunk,
    load_selected_continuation, merge_continuation_chunks,
)
from elspeth.web.composer.continuation_runtime import restore_continuation_runtime
from elspeth.web.composer.continuation_semantic_bindings import current_continuation_semantic_bindings
from elspeth.web.composer.no_tool_response import parse_no_tool_response
from elspeth.web.required_sql_outcomes import RequiredSQLFinishOnce, RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkSource, RequiredWorkTicket
from elspeth.web.sessions.continuation import ContinuationEvent, StoredContinuationSnapshot

if TYPE_CHECKING:
    from elspeth.contracts.composer_progress import ComposerProgressSink
    from elspeth.web.composer.continuation import ContinuationAdvance
    from elspeth.web.composer.provider_quota import ProviderInvocationOwner
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning


def _end_running(required_work: RequiredWorkBinding) -> ComposerOperationRunning:
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning
    running = required_work.running
    if type(running) is not ComposerOperationRunning:
        raise AuditIntegrityError("END requires the actual current running operation")
    return running


def _end_sql_result[T](
    *, required_work: RequiredWorkBinding, sql: RequiredWorkTicket,
    projection: RequiredWorkTicket, source: RequiredWorkSource,
    outcome: RequiredSQLFinishOnce[T],
    projection_kind: Literal["snapshot", "evidence", "authorization"] = "snapshot",
) -> tuple[T, tuple[BaseException, ...]]:
    required_work.coordinator.validate_continuation_sql_outcome(
        ticket=sql, expected_source=source, actual_outcome=outcome)
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        failures = (outcome.error, *outcome.deferred_cancellations)
        if len(failures) == 1:
            raise failures[0]
        raise BaseExceptionGroup("END SQL owner failure and cancellation", failures)
    if type(outcome) is not RequiredSQLReturned:
        projection.complete_without_submission()
        raise AuditIntegrityError("END application returned no registered actual SQL result")
    projection.begin_projection()
    try:
        if projection_kind == "snapshot":
            if type(outcome.value) is not StoredContinuationSnapshot:
                raise AuditIntegrityError("END projection lost its actual complete selected snapshot")
        elif projection_kind == "evidence":
            from elspeth.web.composer.advisor_output import ContinuationEvidenceRef
            if type(outcome.value) is not tuple or any(type(item) is not ContinuationEvidenceRef for item in outcome.value):
                raise AuditIntegrityError("END projection lost its actual closed SQL evidence tuple")
        elif outcome.value is not True:
            raise AuditIntegrityError("END projection lost its actual current terminal authorization")
    except BaseException as error:
        projection.complete_owned(error)
        if outcome.deferred_cancellations:
            raise BaseExceptionGroup("END projection failure and cancellation", (error, *outcome.deferred_cancellations))
        raise
    else:
        projection.complete_owned()
    return outcome.value, outcome.deferred_cancellations


def _end_deliver_cancellations(cancellations: tuple[BaseException, ...]) -> None:
    if len(cancellations) == 1:
        raise cancellations[0]
    if cancellations:
        raise BaseExceptionGroup("Deferred END owner cancellations", cancellations)


async def _end_selected(
    *, authority: ComposerAsyncOperationAuthority, required_work: RequiredWorkBinding,
    stored: StoredContinuationSnapshot, cancellations: tuple[BaseException, ...] = (),
) -> SelectedContinuationState:
    if type(stored) is not StoredContinuationSnapshot or required_work.running is None:
        raise AuditIntegrityError("END SQL owner lost its complete selected result/current claim")
    selected = await load_selected_continuation(authority=authority, running=required_work.running,
        selected=stored, required_work=required_work)
    _end_deliver_cancellations(cancellations)
    return selected


def selected_end_candidate(selected: SelectedContinuationState) -> tuple[EndFrame, EndContinuationCandidate]:
    ends = tuple(frame for frame in selected.stack.frames if type(frame) is EndFrame)
    if len(ends) != 1:
        raise AuditIntegrityError("END requires exactly one retained complete candidate")
    frame = ends[0]
    value = decode_end_candidate(continuation_chunk(selected.chunks, frame.candidate))
    if value.state != frame.state:
        raise AuditIntegrityError("END candidate changed its exact authored state")
    return frame, value
```

- [ ] Add this exact deterministic preparation method. It replaces no provider authoring or graph mutation. It obtains fresh actual runtime preflight, source-proof and unresolved-interpretation facts from the same existing owners as P2. The complete findings become untrusted chronological context before semantic assessment. An invalid or unavailable finding remains a finding; no old repair-turn cap stops the logical request or authorizes the host to choose a mutation.

```python
async def prepare_continuation_end_candidate(
    *, service: ComposerServiceImpl, operation_authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState, required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner, progress: ComposerProgressSink | None,
) -> SelectedContinuationState:
    from elspeth.web.composer.composition_completion import (
        _FINALIZATION_AUTO_SURFACEABLE_KINDS, _proof_repair_exhausted_validation,
        _source_proof_unavailable_validation,
    )
    from elspeth.web.composer.continuation_driver import restore_continuation_primary_result
    from elspeth.web.composer.continuation_execution import capture_continuation_runtime_owners
    from elspeth.web.composer.tools.generation import MalformedSourceProofClaim, ProofEvaluated, ProofUnavailable
    primary = await restore_continuation_primary_result(service=service, operation_authority=operation_authority,
        selected=selected, required_work=required_work, provider_owner=provider_owner, progress=progress)
    runtime, completion = primary.runtime, primary.call_model.completion
    live, bound = runtime.live, runtime.bindings
    frame = selected.stack.frames[-1]
    if (type(frame) is not ComposeFrame or frame.pending_response is None
        or frame.provider_effect_key is None or frame.provider_input_sha256 is None):
        raise AuditIntegrityError("END entry lost its exact selected primary response")
    if completion.tool_batch.calls and primary.control is None and primary.recovery is None:
        raise AuditIntegrityError("END cannot consume an ordinary issued tool batch")
    kind: Literal["control", "recovery", "reply"] = (
        "control" if primary.control is not None else "recovery" if primary.recovery is not None else "reply")
    candidate_text = (primary.recovery.message if primary.recovery is not None else
                      "" if primary.control is not None else parse_no_tool_response(completion.message.content or "").body)
    feedback: list[str] = []
    runtime_result = None
    if kind == "reply":
        runtime_result = await service._preflight.turn_runtime_preflight(state=live.state,
            user_id=bound.user_id, session_id=bound.session_id, last_runtime_preflight=live.last_runtime_preflight,
            runtime_preflight_cache=live.runtime_preflight_cache, initial_version=live.initial_version,
            session_scope=bound.session_scope, recorder=live.recorder, plugin_snapshot=bound.plugin_snapshot,
            session_operation_context=bound.session_operation_context)
        proof = service._completion._assess_source_proof(live.state, session_id=bound.session_id,
            session_operation_context=bound.session_operation_context)
        if type(proof) in (ProofUnavailable, MalformedSourceProofClaim):
            runtime_result = _source_proof_unavailable_validation(live.state, proof, runtime_result)
        elif type(proof) is ProofEvaluated:
            blocking = tuple(item for item in proof.diagnostics if item["severity"] == "blocking")
            if blocking:
                proof_result = _proof_repair_exhausted_validation(blocking)
                if runtime_result is None:
                    runtime_result = proof_result
                else:
                    runtime_result = runtime_result.model_copy(update={"is_valid": False,
                        "checks": [*runtime_result.checks, *proof_result.checks],
                        "errors": [*runtime_result.errors, *proof_result.errors],
                        "readiness": runtime_result.readiness.model_copy(update={"execution_ready": False,
                            "completion_ready": False,
                            "blockers": [*runtime_result.readiness.blockers, *proof_result.readiness.blockers]})})
        missing = await service._interpretation_surfacing._missing_pending_interpretation_review_sites(
            live.state, session_id=bound.session_id)
        model_required = tuple(site for site in missing if site[2] not in _FINALIZATION_AUTO_SURFACEABLE_KINDS)
        if model_required:
            feedback.append(canonical_json({"kind": "unresolved_interpretation_sites",
                "sites": [[site[0], site[1], site[2].value] for site in model_required],
                "instruction": "Review these actual unresolved sites against the complete request. Only the authoring model may choose configuration or interpretation changes."}))
        if runtime_result is not None:
            feedback.append(canonical_json({"kind": "current_runtime_preflight",
                "result": runtime_result.model_dump(mode="json")}))
        live.last_runtime_preflight = runtime_result
    superseded_index = None
    original_reply = completion.message.content or ""
    if kind == "reply" and original_reply.strip():
        superseded_index = len(live.llm_messages)
        live.llm_messages.append({"role": "assistant", "content": original_reply})
    for content in feedback:
        live.llm_messages.append({"role": "user", "content": content})
    stack, values = capture_continuation_runtime_owners(runtime=runtime, stack=selected.stack, chunks=selected.chunks)
    compose = nearest_compose_frame(stack)
    candidate = EndContinuationCandidate(kind=kind, response=frame.pending_response,
        primary_effect_key=frame.provider_effect_key, primary_input_sha256=frame.provider_input_sha256,
        candidate_text=candidate_text, state=compose.state, evidence=compose.evidence, iteration=compose.iteration,
        superseded_reply_index=superseded_index, preparation_feedback=tuple(feedback),
        runtime_preflight_json=None if runtime_result is None else canonical_json(runtime_result.model_dump(mode="json")))
    chunk = encode_end_candidate(candidate)
    compose = compose.model_copy(update={"phase": "action_ready", "pending_response": None,
        "provider_effect_key": None, "provider_input_sha256": None})
    following = stack.model_copy(update={"frames": (*stack.frames[:-1], compose,
        EndFrame(phase="review_needed", state=compose.state, candidate=chunk.reference, publication_binding=None))})
    values = merge_continuation_chunks(values, (chunk,))
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL,
        RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(sql, operation_authority.commit_continuation_end_entry,
            _end_running(required_work), expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.stack.cursor,
            snapshot_json=encode_continuation_snapshot(following), chunks=values)
    except BaseException:
        projection.complete_without_submission()
        raise
    stored, cancellations = _end_sql_result(required_work=required_work, sql=sql, projection=projection,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL, outcome=outcome)
    return await _end_selected(authority=operation_authority, required_work=required_work,
        stored=stored, cancellations=cancellations)
```

- [ ] Add this complete transaction-owning method to the actual authority. It proves the primary response before the caller phase changes, re-parses the sole control/recovery input and retains the exact candidate with the complete caller owners. It allows only owed pure response application after Stop or segment expiry; no handler, new provider or graph mutation is dispatched here.

```python
def commit_continuation_end_entry(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int,
    snapshot_json: str, chunks: tuple[PrivateChunk, ...],
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation import ComposeFrame, EndFrame
    from elspeth.web.composer.continuation_codec import decode_continuation_response
    from elspeth.web.composer.continuation_end_values import decode_end_candidate
    from elspeth.web.composer.continuation_execution import continuation_chunk
    from elspeth.web.composer.no_tool_response import parse_no_tool_response
    from elspeth.web.composer.recovery_controls import parse_continuation_tool_batch, parse_recovery_tool_batch
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    with locked_session_transaction(self._engine, sid) as conn:
        stored = require_snapshot_cas(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        prior, following = decode_continuation_snapshot(stored.payload_json), decode_continuation_snapshot(snapshot_json)
        top = prior.frames[-1]
        if (type(top) is not ComposeFrame or top.phase != "response_retained" or top.pending_response is None
            or top.provider_effect_key is None or top.provider_input_sha256 is None
            or len(following.frames) != len(prior.frames)+1 or type(following.frames[-1]) is not EndFrame
            or type(following.frames[-2]) is not ComposeFrame or following.cursor != prior.cursor):
            raise AuditIntegrityError("END entry changed its selected complete primary caller")
        require_retained_provider_application_on_connection(conn, running, stored=stored,
            effect_key=top.provider_effect_key, input_sha256=top.provider_input_sha256, response=top.pending_response)
        old_values = load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=prior)
        actual = decode_continuation_response(continuation_chunk(old_values, top.pending_response))
        control, recovery = parse_continuation_tool_batch(actual.tool_batch), parse_recovery_tool_batch(actual.tool_batch)
        if actual.tool_batch.calls and control is None and recovery is None:
            raise AuditIntegrityError("END entry substituted an ordinary tool batch")
        end, compose = following.frames[-1], following.frames[-2]
        candidate = decode_end_candidate(continuation_chunk(chunks, end.candidate))
        expected_kind = "control" if control is not None else "recovery" if recovery is not None else "reply"
        expected_text = recovery.message if recovery is not None else "" if control is not None else parse_no_tool_response(actual.message.content or "").body
        if (candidate.kind != expected_kind or candidate.candidate_text != expected_text
            or candidate.response != top.pending_response
            or (candidate.primary_effect_key, candidate.primary_input_sha256) != (top.provider_effect_key, top.provider_input_sha256)
            or candidate.state != compose.state or end.state != compose.state
            or candidate.evidence != compose.evidence or candidate.iteration != compose.iteration
            or candidate.publication_source is not None or candidate.semantic_source is not None
            or compose.phase != "action_ready" or compose.pending_response is not None
            or compose.provider_effect_key is not None or compose.provider_input_sha256 is not None
            or following.logical != prior.logical or following.policy_hash != prior.policy_hash
            or following.admission_policy_json != prior.admission_policy_json
            or following.effect_frontier != prior.effect_frontier or following.pending_settlement_ids != prior.pending_settlement_ids
            or following.pending_owner_results != prior.pending_owner_results or following.semantic_frontier != prior.semantic_frontier
            or following.active_action_json != prior.active_action_json or following.active_action_source != prior.active_action_source):
            raise AuditIntegrityError("END entry altered actual outcome or another execution owner")
        put_private_chunks(conn, session_id=sid, operation_id=operation_id, values=chunks)
        load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=following)
        return append_complete_snapshot(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor, snapshot_json=snapshot_json,
            event_batch=(ContinuationEvent("review_retained", canonical_json({"kind": "end_candidate", "candidate": end.candidate.digest})),), handoff=False)
```

The source-proof helper's historical name contains “exhausted”; v2 uses only its exact deterministic diagnostics-to-readiness projection. It does not consume or set a semantic retry cap. The existing public failure/readiness bytes and source identity checks remain intact.

## E3: Dispatch publication and apply its actual pass once

**Modify:** `composer/continuation_end.py`, `coordination/composer_operation_authority.py`; use storage C5.9's actual shared pass writer. The pure E1 candidate and F13 source codecs precede C5.9; these runtime adapters follow C5/F21.

- [ ] Add this complete source builder/dispatch entry. Its candidate locator hashes the exact authored text, rather than a chunk containing changing audit metadata. The existing physical F21 owner prepares and dispatches the actual request after the driver fit check. A prescribed `review` owns one outer prepared effect; nested physical Advisor calls do not each claim to execute that semantic action.

```python
async def push_continuation_end_review(
    *, service: ComposerServiceImpl, operation_authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState, required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner, progress: ComposerProgressSink | None,
    semantic: bool,
) -> SelectedContinuationState:
    from elspeth.web.composer.advisor_output import ReviewContinuationAction
    from elspeth.web.composer.continuation_actions import continuation_premise_input_hash
    from elspeth.web.composer.continuation_progress import decode_prescribed_continuation_action
    from elspeth.web.composer.continuation_execution import capture_continuation_runtime_owners
    end, candidate = selected_end_candidate(selected)
    if selected.stack.frames[-1] != end or candidate.kind == "control":
        raise AuditIntegrityError("automatic END review lost its selected non-scheduling candidate")
    runtime = await restore_continuation_runtime(service=service, operation_authority=operation_authority,
        checkpoint_id=selected.checkpoint_id, stack=selected.stack, chunks=selected.chunks,
        required_work=required_work, provider_owner=provider_owner, progress=progress)
    live, bound = runtime.live, runtime.bindings
    review = live.turn_evidence.review_candidate(candidate=candidate.candidate_text,
        request=bound.message, current_state=live.state)
    policy_hash, manifest_hash, permitted_reads = current_continuation_semantic_bindings(service, runtime)
    if selected.stack.active_action_json is not None:
        action = decode_prescribed_continuation_action(selected.stack.active_action_json)
        if (semantic or type(action) is not ReviewContinuationAction
            or action.candidate_ref != continuation_candidate_id(candidate) or action.evidence_binding != review.binding
            or (action.policy_hash, action.tool_manifest_hash) != (policy_hash, manifest_hash)):
            raise AuditIntegrityError("prescribed review changed its current actual candidate/security binding")
        sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL,
            RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
        try:
            outcome = await run_required_sql_finish_once(sql, operation_authority.prepare_continuation_review,
                _end_running(required_work), expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.stack.cursor,
                candidate_ref=continuation_candidate_id(candidate), coverage_binding=review.binding)
        except BaseException:
            projection.complete_without_submission()
            raise
        stored, cancellations = _end_sql_result(required_work=required_work, sql=sql, projection=projection,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL, outcome=outcome)
        selected = await _end_selected(authority=operation_authority, required_work=required_work,
            stored=stored, cancellations=cancellations)
    premise = None
    stack, values = selected.stack, selected.chunks
    if semantic:
        if stack.semantic_frontier is None or stack.active_action_json is not None:
            raise AuditIntegrityError("semantic assessment precedes initialization or prior action settlement")
        sql, projection = required_work.reserve_pair(RequiredWorkSource.RETAINED_OPERATION_READ_SQL,
            RequiredWorkSource.RETAINED_OPERATION_READ_PROJECTION)
        try:
            outcome = await run_required_sql_finish_once(sql, operation_authority.resolve_continuation_frontier_evidence,
                _end_running(required_work), expected_checkpoint_id=selected.checkpoint_id, expected_cursor=stack.cursor)
        except BaseException:
            projection.complete_without_submission()
            raise
        actual_evidence, cancellations = _end_sql_result(required_work=required_work, sql=sql, projection=projection,
            source=RequiredWorkSource.RETAINED_OPERATION_READ_SQL, outcome=outcome, projection_kind="evidence")
        frontier = decode_frontier_chunk(continuation_chunk(values, stack.semantic_frontier))
        from elspeth.web.composer.turn_effects import authored_state_hash
        frontier = replace(frontier, evidence=actual_evidence, graph_hash=authored_state_hash(live.state))
        frontier_chunk = encode_frontier_chunk(frontier)
        stack = stack.model_copy(update={"semantic_frontier": frontier_chunk.reference})
        values = merge_continuation_chunks(values, (frontier_chunk,))
        premise = ContinuationAdvisorPremise(
            input_hash=continuation_premise_input_hash(logical=stack.logical, frontier=frontier,
                policy_hash=policy_hash, tool_manifest_hash=manifest_hash),
            policy_hash=policy_hash, tool_manifest_hash=manifest_hash, frontier=frontier_chunk.reference)
        live.llm_messages.append({"role": "user", "content": canonical_json({
            "kind": "continuation_candidate", "candidate_ref": continuation_candidate_id(candidate),
            "evidence_binding": review.binding, "publication_admitted": end.publication_binding == review.binding,
            "permitted_read_tools": sorted(permitted_reads)})})
        _end_deliver_cancellations(cancellations)
    stack, values = capture_continuation_runtime_owners(runtime=runtime, stack=stack, chunks=values)
    compose = nearest_compose_frame(stack)
    source = ContinuationAdvisorInvocation(kind="semantic" if semantic else "publication",
        state=compose.state, evidence=compose.evidence, transcript=compose.transcript, iteration=compose.iteration,
        coverage_binding=review.binding, manual_arguments_json=None, manual_tool_call_id=None,
        candidate=candidate.candidate_text, premise=premise)
    source_chunk = encode_continuation_advisor_invocation(source)
    values = merge_continuation_chunks(values, (source_chunk,))
    stack = stack.model_copy(update={"frames": (*stack.frames, AdvisorFrame(phase="input_ready",
        review=source_chunk.reference, response=None, coverage_binding=review.binding))})
    return await commit_continuation_phase(authority=operation_authority, running=_end_running(required_work),
        checkpoint_id=selected.checkpoint_id, previous_cursor=selected.stack.cursor, stack=stack,
        chunks=values, required_work=required_work,
        event=ContinuationEvent("review_retained", canonical_json({"kind": source.kind, "review": source_chunk.reference.digest})))


def prepare_continuation_review(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int,
    candidate_ref: str, coverage_binding: str,
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.advisor_output import ReviewContinuationAction, parse_continuation_advisor_decision
    from elspeth.web.composer.continuation_codec import decode_continuation_response, decode_frontier_chunk
    from elspeth.web.composer.continuation_end_values import continuation_candidate_id
    from elspeth.web.composer.continuation_end_values import decode_end_candidate
    from elspeth.web.composer.continuation_execution import continuation_chunk
    from elspeth.web.composer.continuation_progress import continuation_action_key, decode_prescribed_continuation_action
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    with locked_session_transaction(self._engine, sid) as conn:
        require_continuation_mutation(conn, running.session_operation_context)
        stored = require_snapshot_cas(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        root = decode_continuation_snapshot(stored.payload_json)
        if type(root.frames[-1]) is not EndFrame or root.active_action_json is None or root.active_action_source is None or root.semantic_frontier is None:
            raise AuditIntegrityError("prescribed review has no exact active candidate/source/frontier")
        values = load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=root)
        candidate = decode_end_candidate(continuation_chunk(values, root.frames[-1].candidate))
        action = decode_prescribed_continuation_action(root.active_action_json)
        source = root.active_action_source
        actual_id = require_retained_provider_application_on_connection(conn, running, stored=stored,
            effect_key=source.effect_key, input_sha256=source.input_sha256, response=source.response)
        actual = decode_continuation_response(continuation_chunk(values, source.response))
        decision = parse_continuation_advisor_decision(actual.message.content, actual_advisor_call_id=actual_id)
        prescribed = None if decision is None else decision.prescribed_action
        if (type(action) is not ReviewContinuationAction or type(prescribed) is not ReviewContinuationAction
            or (action.candidate_ref, action.evidence_binding) != (candidate_ref, coverage_binding)
            or (prescribed.candidate_ref, prescribed.evidence_binding) != (candidate_ref, coverage_binding)
            or candidate_ref != continuation_candidate_id(candidate)):
            raise AuditIntegrityError("outer review differs from its exact actual prescribing response")
        frontier = decode_frontier_chunk(continuation_chunk(values, root.semantic_frontier))
        key = "action:review:" + continuation_action_key(action, retained_evidence={item.receipt_id: item for item in frontier.evidence})
        input_sha = stable_hash({"schema": "composer.prescribed-review-input.v1", "logical": root.logical.model_dump(mode="json"),
            "action": action.model_dump(mode="json"), "candidate": root.frames[-1].candidate.digest,
            "coverage_binding": coverage_binding})
        previous = conn.execute(select(effects).where(effects.c.session_id == sid,
            effects.c.operation_id == operation_id, effects.c.effect_key == key)).one_or_none()
        if previous is not None:
            if (previous.owner, previous.input_sha256, previous.action_json, previous.disposition) != (
                    "session_sql", input_sha, root.active_action_json, "prepared"):
                raise AuditIntegrityError("outer review preparation changed a retained actual owner")
            return stored
        return prepare_effect_on_connection(conn, running, effect_key=key, input_sha256=input_sha,
            owner="session_sql", next_snapshot_json=stored.payload_json, expected_checkpoint_id=stored.checkpoint_id,
            expected_cursor=stored.cursor, action_owner="review")
```

Place `prepare_continuation_review` on the actual authority; its SQL/module imports are the existing C3/C5 helpers and concrete frame types. The runtime source builder remains in `continuation_end.py`. Both owners import the pure candidate ID function from E1's `continuation_end_values.py`; the SQL module never imports the runtime dispatcher.

- [ ] Add this complete received-publication application. It preserves the existing actual verdict parser, exact pass index, review history and model-authored findings. Storage C5.9 regenerates the record independently and writes audit+snapshot+outer-review completion together. No second pass-audit write or physical call occurs during cold application.

```python
async def apply_continuation_publication_review(
    *, service: ComposerServiceImpl, operation_authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState, required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner, progress: ComposerProgressSink | None,
) -> SelectedContinuationState:
    from elspeth.web.composer import advisor_policy
    from elspeth.web.composer.advisor_audit import AdvisorCheckpointPassRecord
    from elspeth.web.composer.advisor_checkpoint import _parse_advisor_checkpoint_guidance
    from elspeth.web.composer.composition_completion import _advance_advisor_review_state
    from elspeth.web.composer.continuation import ContinuationActionSource
    from elspeth.web.composer.continuation_execution import capture_continuation_runtime_owners
    child = selected.stack.frames[-1]
    if type(child) is not AdvisorFrame or child.phase != "response_retained" or child.response is None:
        raise AuditIntegrityError("publication application requires its exact received Advisor child")
    source = decode_continuation_advisor_invocation(continuation_chunk(selected.chunks, child.review))
    if source.kind != "publication" or child.provider_effect_key is None or child.provider_input_sha256 is None:
        raise AuditIntegrityError("publication application selected a different actual physical phase")
    end, candidate = selected_end_candidate(selected)
    runtime = await restore_continuation_runtime(service=service, operation_authority=operation_authority,
        checkpoint_id=selected.checkpoint_id, stack=selected.stack, chunks=selected.chunks,
        required_work=required_work, provider_owner=provider_owner, progress=progress)
    live, bound = runtime.live, runtime.bindings
    review = live.turn_evidence.review_candidate(candidate=candidate.candidate_text, request=bound.message, current_state=live.state)
    if (source.candidate != candidate.candidate_text or source.coverage_binding != review.binding
        or child.coverage_binding != review.binding):
        raise AuditIntegrityError("publication application changed the actual reviewed candidate/evidence")
    retained = decode_continuation_response(continuation_chunk(selected.chunks, child.response))
    if type(retained.message.content) is not str:
        raise AuditIntegrityError("publication response lacks actual admitted text")
    verdict = _parse_advisor_checkpoint_guidance(retained.message.content, phase="end")
    pass_index = live.advisor_checkpoint_passes_used + 1
    record = AdvisorCheckpointPassRecord.from_findings(phase="end", pass_index=pass_index,
        verdict=("flagged" if verdict.blocking else "clean") if verdict.ok else "malformed", source="model",
        findings_text=verdict.findings_text, provider_attempts=1,
        first_attempt_schema_valid=verdict.response_schema_valid,
        first_attempt_accepted=verdict.ok if verdict.response_schema_valid is not None else None,
        format_reprompt_sent=False, step_ids_offered=len(verdict.affected_step_ids) if verdict.ok else None,
        step_ids_kept=len(advisor_policy.validated_advisor_step_ids(live.state, verdict.affected_step_ids)) if verdict.ok else None,
        note_present=verdict.response_note_present if verdict.ok else None,
        url_redactions=verdict.url_redactions if verdict.ok else None, email_redactions=verdict.email_redactions if verdict.ok else None,
        publication_verdict=verdict.publication_verdict if verdict.ok else None,
        publication_binding=review.binding if verdict.ok else None)
    live.advisor_review_state = _advance_advisor_review_state(live.advisor_review_state,
        verdict=verdict, evidence_hash=review.semantic_repair_key, pass_index=pass_index)
    live.advisor_checkpoint_passes_used = pass_index
    live.llm_messages.append({"role": "user", "content": canonical_json({
        "kind": "actual_publication_review", "binding": review.binding, "pass": record.to_dict(),
        "findings": verdict.findings_text})})
    admitted = verdict.ok and verdict.publication_verdict == "ADMIT"
    if not admitted:
        live.recorder.record_withheld_reply(origin="advisor_publication_withheld", content=candidate.candidate_text)
    stack, values = capture_continuation_runtime_owners(runtime=runtime, stack=selected.stack, chunks=selected.chunks)
    actual_source = ContinuationActionSource(effect_key=child.provider_effect_key,
        input_sha256=child.provider_input_sha256, response=child.response)
    updated = candidate.model_copy(update={"publication_binding": review.binding if admitted else None,
        "publication_source": actual_source, "publication_record_json": canonical_json(record.to_dict()),
        "semantic_source": None, "semantic_decision_json": None})
    candidate_chunk = encode_end_candidate(updated)
    next_end = end.model_copy(update={"candidate": candidate_chunk.reference,
        "phase": "publication_ready" if admitted else "repair_needed",
        "publication_binding": review.binding if admitted else None})
    stack = stack.model_copy(update={"frames": (*stack.frames[:-2], next_end)})
    values = merge_continuation_chunks(values, (candidate_chunk,))
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL,
        RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
    try:
        outcome = await service._require_sessions_service().persist_continuation_advisor_pass_finish_once(
            running=_end_running(required_work), expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.stack.cursor, snapshot_json=encode_continuation_snapshot(stack), chunks=values,
            effect_key=child.provider_effect_key, input_sha256=child.provider_input_sha256,
            response=child.response, record=record, required_work=sql)
    except BaseException:
        projection.complete_without_submission()
        raise
    stored, cancellations = _end_sql_result(required_work=required_work, sql=sql, projection=projection,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL, outcome=outcome)
    return await _end_selected(authority=operation_authority, required_work=required_work,
        stored=stored, cancellations=cancellations)
```

The shared writer owns the real audit row. Any configured telemetry mirror runs only after this returned SQL result; telemetry failure never causes a second durable pass write. The buffered withheld-reply channel is the real F6 private channel, later persisted through the existing terminal audit owner.

## E4: Accept only the actual received, currently bound semantic decision

**Modify:** `coordination/composer_operation_authority.py`, `composer/continuation_end.py`; extend C6's resolver for the closed review receipt variant in the main plan. This is a product action receipt, not a plan approval flow.

- [ ] Add this complete authority method. It derives the next root from its selected actual SQL root, not a precomputed caller payload blessed with a newer checkpoint. The model never supplies its physical decision ID. A whole Stop must have current actual publication admission, complete structural/effect coverage, and the closed reducer's whole-request obligation checks. Current semantic security bindings are supplied by E0's actual current service producer immediately before this call; every later dispatched action independently rechecks them and its resource/guard policy.

```python
def accept_continuation_advisor_decision(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int,
    current_policy_hash: str, current_tool_manifest_hash: str, permitted_read_tools: frozenset[str],
) -> StoredContinuationSnapshot:
    from dataclasses import replace
    from elspeth.web.composer.advisor_audit import AdvisorCheckpointPassRecord
    from elspeth.web.composer.advisor_checkpoint import _parse_advisor_checkpoint_guidance
    from elspeth.web.composer.advisor_output import ReviewContinuationAction, parse_continuation_advisor_decision
    from elspeth.web.composer.continuation import AdvisorFrame, ContinuationActionSource, EndFrame
    from elspeth.web.composer.continuation_actions import continuation_premise_input_hash
    from elspeth.web.composer.continuation_advisor import decode_continuation_advisor_invocation
    from elspeth.web.composer.continuation_codec import decode_continuation_response, decode_frontier_chunk, encode_frontier_chunk
    from elspeth.web.composer.continuation_end_values import decode_end_candidate, encode_end_candidate
    from elspeth.web.composer.continuation_execution import continuation_chunk
    from elspeth.web.composer.continuation_progress import ContinuationProtocolError, retain_advisor_action
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    with locked_session_transaction(self._engine, sid) as conn:
        require_continuation_mutation(conn, running.session_operation_context, allow_cancel=True, allow_expired_segment=True)
        stored = require_snapshot_cas(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        root = decode_continuation_snapshot(stored.payload_json)
        if (len(root.frames) < 3 or type(root.frames[-1]) is not AdvisorFrame
            or type(root.frames[-2]) is not EndFrame or root.active_action_json is not None):
            raise AuditIntegrityError("semantic acceptance lost its actual selected child/candidate or prior action")
        child, end = root.frames[-1], root.frames[-2]
        if (child.phase != "response_retained" or child.response is None
            or child.provider_effect_key is None or child.provider_input_sha256 is None):
            raise AuditIntegrityError("semantic acceptance has no complete actual physical response")
        values = load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=root)
        source = decode_continuation_advisor_invocation(continuation_chunk(values, child.review))
        candidate = decode_end_candidate(continuation_chunk(values, end.candidate))
        if (source.kind != "semantic" or source.premise is None or root.semantic_frontier != source.premise.frontier
            or source.candidate != candidate.candidate_text or source.state != candidate.state
            or (source.premise.policy_hash, source.premise.tool_manifest_hash) != (current_policy_hash, current_tool_manifest_hash)):
            raise ContinuationProtocolError("The received assessment's current semantic premise changed")
        actual_id = require_retained_provider_application_on_connection(conn, running, stored=stored,
            effect_key=child.provider_effect_key, input_sha256=child.provider_input_sha256, response=child.response)
        retained = decode_continuation_response(continuation_chunk(values, child.response))
        if type(retained.message.content) is not str or retained.tool_batch.calls:
            raise ContinuationProtocolError("The actual semantic response is not one admitted decision")
        decision = parse_continuation_advisor_decision(retained.message.content, actual_advisor_call_id=actual_id)
        if decision is None:
            raise ContinuationProtocolError("The actual Advisor response does not satisfy the continuation decision schema")
        frontier = decode_frontier_chunk(continuation_chunk(values, source.premise.frontier))
        current_evidence = resolve_frontier_evidence_on_connection(conn, running,
            expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor)
        by_current_id = {item.receipt_id: item for item in current_evidence}
        if any(by_current_id.get(item.receipt_id) != item for item in frontier.evidence):
            raise AuditIntegrityError("semantic assessment changed an actual retained evidence premise")
        expected_input = continuation_premise_input_hash(logical=root.logical, frontier=frontier,
            policy_hash=current_policy_hash, tool_manifest_hash=current_tool_manifest_hash)
        if expected_input != source.premise.input_hash:
            raise AuditIntegrityError("semantic assessment changed its exact prepared input premise")
        publication_admitted = False
        if candidate.publication_source is not None and candidate.publication_record_json is not None:
            publication = candidate.publication_source
            require_retained_provider_application_on_connection(conn, running, stored=stored,
                effect_key=publication.effect_key, input_sha256=publication.input_sha256, response=publication.response)
            actual_publication = decode_continuation_response(continuation_chunk(values, publication.response))
            verdict = _parse_advisor_checkpoint_guidance(actual_publication.message.content, phase="end")
            record = AdvisorCheckpointPassRecord.from_dict(parse_json_strict(candidate.publication_record_json)[0])
            publication_admitted = (verdict.ok and verdict.publication_verdict == "ADMIT"
                and record.publication_verdict == "ADMIT" and record.publication_binding is not None
                and record.publication_binding == candidate.publication_binding == end.publication_binding
                and source.coverage_binding == candidate.publication_binding)
        complete = (publication_admitted and not root.pending_owner_results and not root.pending_settlement_ids
            and not any(item.disposition in ("prepared", "dispatched", "unknown") for item in root.effect_frontier))
        actual_source = ContinuationActionSource(effect_key=child.provider_effect_key,
            input_sha256=child.provider_input_sha256, response=child.response)
        accepted, frontier_chunk = retain_advisor_action(root, decision, actual_advisor_call_id=actual_id,
            actual_source=actual_source, frontier=frontier,
            retained_evidence={item.receipt_id: item for item in frontier.evidence}, input_hash=expected_input,
            policy_hash=current_policy_hash, tool_manifest_hash=current_tool_manifest_hash,
            permitted_read_tools=permitted_read_tools, complete_required_coverage=complete)
        if decision.prescribed_action is None and not (decision.decision == "STOP_CONFIRMED" and decision.scope == "whole_request"):
            raise ContinuationProtocolError("A subset assessment cannot end the whole logical request")
        accepted_frontier = decode_frontier_chunk(frontier_chunk)
        # The received decision could not cite its own future receipt. Retain
        # that actual observation after checking the exact supplied context.
        frontier_chunk = encode_frontier_chunk(replace(accepted_frontier, evidence=current_evidence))
        updated_candidate = candidate.model_copy(update={"semantic_source": actual_source,
            "semantic_decision_json": canonical_json(decision.model_dump(mode="json"))})
        candidate_chunk = encode_end_candidate(updated_candidate)
        next_end = end.model_copy(update={"candidate": candidate_chunk.reference})
        action = decision.prescribed_action
        if action is not None and type(action) is not ReviewContinuationAction:
            frames = accepted.frames[:-2]
        else:
            frames = (*accepted.frames[:-2], next_end)
        following = accepted.model_copy(update={"frames": frames, "semantic_frontier": frontier_chunk.reference})
        put_private_chunks(conn, session_id=sid, operation_id=operation_id, values=(frontier_chunk, candidate_chunk))
        load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=following)
        return append_complete_snapshot(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
            snapshot_json=encode_continuation_snapshot(following),
            event_batch=(ContinuationEvent("review_retained", canonical_json({"kind": "semantic",
                "effect_key": child.provider_effect_key, "physical_call_id": actual_id, "decision": decision.decision})),), handoff=False)


async def apply_continuation_semantic_review(
    *, service: ComposerServiceImpl, operation_authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState, required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner, progress: ComposerProgressSink | None,
) -> SelectedContinuationState:
    runtime = await restore_continuation_runtime(service=service, operation_authority=operation_authority,
        checkpoint_id=selected.checkpoint_id, stack=selected.stack, chunks=selected.chunks,
        required_work=required_work, provider_owner=provider_owner, progress=progress)
    policy_hash, manifest_hash, permitted_reads = current_continuation_semantic_bindings(service, runtime)
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL,
        RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(sql, operation_authority.accept_continuation_advisor_decision,
            _end_running(required_work), expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.stack.cursor,
            current_policy_hash=policy_hash, current_tool_manifest_hash=manifest_hash, permitted_read_tools=permitted_reads)
    except BaseException:
        projection.complete_without_submission()
        raise
    stored, cancellations = _end_sql_result(required_work=required_work, sql=sql, projection=projection,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL, outcome=outcome)
    return await _end_selected(authority=operation_authority, required_work=required_work,
        stored=stored, cancellations=cancellations)
```

Place the synchronous method on the actual authority and the async adapter in `continuation_end.py`; the latter uses E2's complete imports. Import the complete pure decision/review codecs in the SQL owner normally. `parse_json_strict` is its existing C3 import. The accepted typed decision is retained exactly, but only the original admitted wire answer plus actual SQL call ID can create it. A replay at the old head refuses before modifying frontier/history or allocating another ordinal.

## E5: Keep finalization, durable Stop and scheduling on their actual owners

**Modify:** `composer/continuation_end.py`, `sessions/continuation.py`, `coordination/composer_operation_authority.py`; add the terminal guard to C3.10's existing transaction.

- [ ] Add this complete terminal authorization check to `sessions/continuation.py`. Call it in `settle_continuation_on_connection` immediately after loading the complete selected `stored`, only for a non-error outcome. The existing outer assistant/state/recovery/audit transaction remains the publication owner: refusal rolls all its earlier writes back. This guard does not manufacture a result or change a failure into a success.

```python
def require_continuation_terminal_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, stored: StoredContinuationSnapshot,
) -> None:
    from elspeth.web.composer.advisor_checkpoint import _parse_advisor_checkpoint_guidance
    from elspeth.web.composer.advisor_output import parse_continuation_advisor_decision
    from elspeth.web.composer.continuation import EndFrame
    from elspeth.web.composer.continuation_codec import decode_continuation_response, decode_frontier_chunk
    from elspeth.web.composer.continuation_end_values import decode_end_candidate
    from elspeth.web.composer.continuation_execution import continuation_chunk
    from elspeth.web.composer.continuation_progress import current_action_receipts
    root = decode_continuation_snapshot(stored.payload_json)
    if (type(root.frames[-1]) is not EndFrame or root.frames[-1].phase != "publication_ready"
        or root.active_action_json is not None or root.active_action_source is not None
        or root.pending_owner_results or root.pending_settlement_ids or root.semantic_frontier is None
        or any(item.disposition in ("prepared", "dispatched", "unknown") for item in root.effect_frontier)):
        raise AuditIntegrityError("logical success precedes current publication and whole-request settlement")
    values = load_private_chunks(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id, stack=root)
    end = root.frames[-1]
    candidate = decode_end_candidate(continuation_chunk(values, end.candidate))
    if (candidate.kind == "control" or candidate.publication_source is None or candidate.semantic_source is None
        or candidate.semantic_decision_json is None or candidate.publication_binding is None
        or candidate.publication_binding != end.publication_binding):
        raise AuditIntegrityError("logical success lacks its exact actual reviewed candidate/assessment")
    publication = candidate.publication_source
    require_retained_provider_application_on_connection(conn, running, stored=stored,
        effect_key=publication.effect_key, input_sha256=publication.input_sha256, response=publication.response)
    actual_publication = decode_continuation_response(continuation_chunk(values, publication.response))
    verdict = _parse_advisor_checkpoint_guidance(actual_publication.message.content, phase="end")
    semantic = candidate.semantic_source
    call_id = require_retained_provider_application_on_connection(conn, running, stored=stored,
        effect_key=semantic.effect_key, input_sha256=semantic.input_sha256, response=semantic.response)
    actual_semantic = decode_continuation_response(continuation_chunk(values, semantic.response))
    decision = parse_continuation_advisor_decision(actual_semantic.message.content, actual_advisor_call_id=call_id)
    frontier = decode_frontier_chunk(continuation_chunk(values, root.semantic_frontier))
    if (not verdict.ok or verdict.publication_verdict != "ADMIT" or decision is None
        or canonical_json(decision.model_dump(mode="json")) != candidate.semantic_decision_json
        or decision.decision != "STOP_CONFIRMED" or decision.scope != "whole_request" or decision.prescribed_action is not None
        or any(item.status == "open" for item in frontier.obligations) or frontier.pending_review_bindings
        or any(item.disposition in ("prepared", "dispatched", "unknown") for item in current_action_receipts(frontier.action_receipts))):
        raise AuditIntegrityError("logical success lost actual current ADMIT or bound whole-request Stop")
    current = resolve_frontier_evidence_on_connection(conn, running,
        expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor)
    if frontier.evidence != current:
        raise AuditIntegrityError("logical success changed actual retained evidence after its assessment")
```

The exact added call in C3.10 is:

```python
if not failed:
    require_continuation_terminal_on_connection(conn, running, stored=stored)
```

- [ ] Add this complete final-result adapter. It invokes the existing actual deterministic tail only after the actual semantic whole Stop is retained. It preserves pending-interpretation masked validation, backend surfacing followed by the unfiltered orphan gate, exact raw/content augmentation, current graph signoff and recovery intent. The existing tail contains no primary/Advisor SDK call. Its backend interpretation writer reconciles the same current reviewed artifact; frames F35 supplies the actual pending-card cold-replay oracle. It emits “complete” only in this authorized finalization stage.

```python
async def finalize_continuation_end(
    *, service: ComposerServiceImpl, operation_authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState, required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner, progress: ComposerProgressSink | None,
) -> ContinuationAdvance:
    from elspeth.web.composer._compose_loop_carriers import _AdmittedAssistantMessage
    from elspeth.web.composer.advisor_audit import AdvisorGraphReviewIdentity
    from elspeth.web.composer.advisor_checkpoint import _parse_advisor_checkpoint_guidance
    from elspeth.web.composer.advisor_decision import AdvisorGatePassed
    from elspeth.web.composer.composition_completion import _with_advisor_gate_decision
    from elspeth.web.composer.continuation import ContinuationAdvance
    from elspeth.web.composer.no_tool_policy import is_pending_interpretation_handoff, state_is_structurally_empty
    from elspeth.web.execution.completion_gates import AdvisorBlockCause, completion_gate_fingerprint
    from elspeth.web.execution.schemas import ValidationResult
    end, candidate = selected_end_candidate(selected)
    if candidate.semantic_source is None or candidate.publication_source is None or candidate.semantic_decision_json is None:
        raise AuditIntegrityError("finalization precedes actual semantic/publication application")
    runtime = await restore_continuation_runtime(service=service, operation_authority=operation_authority,
        checkpoint_id=selected.checkpoint_id, stack=selected.stack, chunks=selected.chunks,
        required_work=required_work, provider_owner=provider_owner, progress=progress)
    live, bound = runtime.live, runtime.bindings
    publication = decode_continuation_response(continuation_chunk(selected.chunks, candidate.publication_source.response))
    verdict = _parse_advisor_checkpoint_guidance(publication.message.content, phase="end")
    if not verdict.ok or verdict.publication_verdict != "ADMIT":
        raise AuditIntegrityError("finalizer has no current actual publication admission")
    current_review = live.turn_evidence.review_candidate(candidate=candidate.candidate_text,
        request=bound.message, current_state=live.state)
    if current_review.binding != end.publication_binding:
        raise AuditIntegrityError("candidate or actual evidence changed after publication review")
    if candidate.superseded_reply_index is not None:
        index = candidate.superseded_reply_index
        original = decode_continuation_response(continuation_chunk(selected.chunks, candidate.response)).message.content or ""
        if (index >= len(live.llm_messages) or live.llm_messages[index]["role"] != "assistant"
            or live.llm_messages[index]["content"] != original or "tool_calls" in live.llm_messages[index]):
            raise AuditIntegrityError("finalization lost its exact once-appended primary reply")
        del live.llm_messages[index]
    assistant = _AdmittedAssistantMessage(content=candidate.candidate_text)
    if candidate.kind == "recovery":
        completion = decode_continuation_response(continuation_chunk(selected.chunks, candidate.response))
        result = service._terminal_recovery_result(completion, live.state, live.recorder,
            prior_tools_persisted=live.persisted_tool_call_turn)
        if result is None:
            raise AuditIntegrityError("retained recovery response lost its actual intent")
    else:
        result = await service._completion._surface_and_finalize_no_tools(
            assistant_message=assistant, no_tool_response=parse_no_tool_response(candidate.candidate_text),
            state=live.state, session_id=bound.session_id, current_state_id=live.current_state_id,
            progress=progress, recorder=live.recorder, initial_version=live.initial_version,
            user_id=bound.user_id, last_runtime_preflight=live.last_runtime_preflight,
            runtime_preflight_cache=live.runtime_preflight_cache, session_scope=bound.session_scope,
            message=bound.message, mutation_success_seen=live.mutation_success_seen,
            repair_turns_used=live.repair_turns_used, session_operation_context=bound.session_operation_context,
            plugin_snapshot=bound.plugin_snapshot, deadline=None)
    actual_runtime = result.runtime_preflight
    if candidate.runtime_preflight_json is not None:
        retained_runtime = ValidationResult.model_validate_json(candidate.runtime_preflight_json, strict=True)
        if not retained_runtime.is_valid and not is_pending_interpretation_handoff(retained_runtime):
            from elspeth.web.composer.no_tool_policy import compose_preflight_failure_message
            result = replace(result, message=compose_preflight_failure_message(candidate.candidate_text, runtime_result=retained_runtime),
                raw_assistant_content=candidate.candidate_text, runtime_preflight=retained_runtime)
            actual_runtime = retained_runtime
    outstanding = None
    if actual_runtime is not None and is_pending_interpretation_handoff(actual_runtime):
        outstanding = await service._preflight.pending_handoff_outstanding_findings(live.state,
            user_id=bound.user_id, session_id=bound.session_id, cache=live.runtime_preflight_cache,
            initial_version=live.initial_version, session_scope=bound.session_scope, llm_calls=live.recorder.llm_calls,
            plugin_snapshot=bound.plugin_snapshot, session_operation_context=bound.session_operation_context)
    graph_blocked = (bound.completion_gates is not None and bound.completion_gates.advisor_signoff is not None
        and bound.completion_gates.advisor_signoff.for_graph == completion_gate_fingerprint(live.state)
        and bound.completion_gates.advisor_signoff.cause is AdvisorBlockCause.GRAPH_REJECTED)
    orphans = await service._interpretation_surfacing._missing_pending_interpretation_review_sites(live.state, session_id=bound.session_id)
    graph_qualified = (actual_runtime is not None and
        (actual_runtime.is_valid or is_pending_interpretation_handoff(actual_runtime) and outstanding is None))
    decision = (AdvisorGatePassed(for_graph=completion_gate_fingerprint(live.state))
        if not verdict.blocking and not graph_blocked and not orphans and graph_qualified
        and not state_is_structurally_empty(live.state) else None)
    identity = AdvisorGraphReviewIdentity(binding=end.publication_binding,
        for_graph=completion_gate_fingerprint(live.state), source="model")
    if verdict.blocking:
        result = service._advisor_checkpoint._advisor_blocked_result(reason="flagged_no_repair", verdict=verdict,
            state=live.state, assistant_message=assistant, recorder=live.recorder,
            repair_turns_used=live.repair_turns_used, persisted_assistant_message_id=live.persisted_assistant_message_id,
            persisted_assistant_content=live.persisted_assistant_content, persisted_tool_call_turn=live.persisted_tool_call_turn,
            runtime_preflight=actual_runtime, outstanding_findings=outstanding,
            publication_binding=end.publication_binding, saved_scope=live.turn_evidence.render_saved_scope())
    result = _with_advisor_gate_decision(result, bound.completion_gates, decision, identity)
    result = replace(result, repair_turns_used=live.repair_turns_used,
        persisted_assistant_message_id=live.persisted_assistant_message_id,
        persisted_assistant_content=live.persisted_assistant_content, persisted_tool_call_turn=live.persisted_tool_call_turn)
    if live.advisor_repair_context_introduced:
        result = await service._completion._qualified_advisor_repair_public_result(result,
            user_id=bound.user_id, session_id=bound.session_id, session_operation_context=bound.session_operation_context,
            cache=live.runtime_preflight_cache, initial_version=live.initial_version, session_scope=bound.session_scope,
            plugin_snapshot=bound.plugin_snapshot, publication_binding=end.publication_binding,
            turn_evidence=live.turn_evidence, user_message=bound.message)
    result = service._completion._append_checkpoint_receipt(result, turn_evidence=live.turn_evidence)
    # The actual result owns this final projection. No fresh continuation
    # snapshot is claimed: a refused terminal retries from the same received
    # candidate and applies no SDK effect twice.
    return ContinuationAdvance(selected.checkpoint_id, selected.stack, selected.chunks, result)
```

The actual returned result and C5's already retained physical audit cohort enter C4's original terminal transaction, where E5 rechecks the SQL-selected root. C4 uses the selected actual P4 graph head and requires `pipeline_commit_intent is None`: H2 retires the only `PlanningApplication` producer of that intent. It never repeats v1's post-compose state save for an already retained P4 graph. The existing `prepare_graph_recovery` only prepares a frozen target/IDs; its actual mutation remains in `_settle_graph_recovery_on_connection` inside the same terminal transaction. If terminal SQL fails, the received END candidate remains selected; cold finalization repeats fresh validation and actual idempotent interpretation reconciliation, never an SDK call. Frames F35's `tests/integration/web/composer/test_continuation_review_handoffs.py::test_actual_review_handoff_cold_cut_preserves_unapplied_user_debt[interpretation_backend-backend_card]` proves the actual card IDs survive the cut. The temporary reply removal changes only the final projection's local shelf; it is not a separately committed continuation phase.

- [ ] Add this exact Stop preparation to the actual authority. The worker reaches it after joining/projecting owed private results; it does not discard a selected batch or an unresolved physical attempt. Only SQL/read effects whose real business transaction never committed can be refused from their actual prepared row. Provider rows are never declared zero-use from this absence.

```python
def prepare_continuation_stop(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int,
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
    from elspeth.web.composer.continuation_codec import encode_private_chunk
    from elspeth.web.sessions.continuation import (
        ContinuationOwnerCommit, attempts, private_chunk_key, require_selected_read_input_on_connection,
    )
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    with locked_session_transaction(self._engine, sid) as conn:
        if not require_continuation_mutation(conn, running.session_operation_context, allow_cancel=True, allow_expired_segment=True):
            raise AuditIntegrityError("Stop requires actual continuation attempt history")
        stored = require_snapshot_cas(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        cancelled = conn.execute(select(jobs.c.cancel_requested_at).where(jobs.c.session_id == sid,
            jobs.c.operation_id == operation_id)).scalar_one()
        root = decode_continuation_snapshot(stored.payload_json)
        current = conn.execute(select(attempts).where(attempts.c.session_id == sid,
            attempts.c.operation_id == operation_id, attempts.c.sequence == running.claim.segment_sequence,
            attempts.c.generation == running.claim.ownership_generation,
            attempts.c.claim_attempt == running.claim.attempt,
            attempts.c.session_operation_id == running.session_operation_context.fence.operation_id,
            attempts.c.session_operation_epoch == running.session_operation_context.fence.operation_epoch,
            attempts.c.session_operation_lease_token == running.session_operation_context.fence.lease_token)).one_or_none()
        if current is None:
            raise AuditIntegrityError("Stop changed its exact current physical/claim binding")
        if cancelled is None:
            raise AuditIntegrityError("Stop preparation has no durable actual Stop marker")
        if root.pending_owner_results or root.pending_settlement_ids or any(type(frame) is ToolBatchFrame for frame in root.frames):
            raise AuditIntegrityError("Stop precedes mandatory issued-result/batch/financial projection")
        rows = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id)).all()
        if any(row.disposition in ("dispatched", "unknown") or row.owner == "provider" and row.disposition == "prepared" for row in rows):
            raise AuditIntegrityError("Stop still owes actual physical outcome reconciliation")
        encoded = stored.payload_json
        prepared = tuple(row for row in rows if row.disposition == "prepared")
        if prepared and (len(prepared) != 1 or type(root.frames[-1]) is not ComposeFrame
            or root.frames[-1].phase != "action_ready" or root.active_action_json is None
            or root.active_action_source is None):
            raise AuditIntegrityError("Stop has no exact selected prescribed-read owner")
        for row in rows:
            if row.disposition != "prepared":
                continue
            if (row.owner != "read" or row.receipt_key is not None or row.result_chunk_key is not None
                or row.tool_receipt_json is not None or row.committer_sequence is not None
                or row.committer_generation is not None or row.committed_state_ids_json is not None
                or row.graph_sha256 is not None or row.provider_attempt_id is not None
                or row.physical_sequence is not None or row.physical_generation is not None
                or row.requested_model is not None or row.response_finished_at is not None
                or row.action_json != root.active_action_json):
                raise AuditIntegrityError("Stop cannot sweep another prepared or actual committed execution owner")
            creator = conn.execute(select(attempts).where(attempts.c.session_id == sid,
                attempts.c.operation_id == operation_id, attempts.c.sequence == row.creator_sequence,
                attempts.c.generation == row.creator_generation)).one_or_none()
            if creator is None:
                raise AuditIntegrityError("prescribed-read Stop lost its immutable actual preparation history")
            commit = ContinuationOwnerCommit(running, row.effect_key, row.input_sha256, stored.checkpoint_id, stored.cursor)
            require_selected_read_input_on_connection(conn, commit, stored)
            frontier = tuple(item for item in root.effect_frontier if item.effect_id == row.effect_key)
            if len(frontier) != 1 or frontier[0].disposition != "prepared" or frontier[0].receipt_id is not None:
                raise AuditIntegrityError("prescribed-read Stop changed its exact selected actual frontier")
            receipt_id = f"stop:{row.effect_key}"
            refusal = encode_private_chunk(codec="composer.owner-result.v1", canonical=canonical_json({
                "schema_version": "composer.prescribed-read-stop.v1", "effect_key": row.effect_key,
                "input_sha256": row.input_sha256, "owner": "read", "reason": "durable_stop_before_result_retention"}))
            put_private_chunks(conn, session_id=sid, operation_id=operation_id, values=(refusal,))
            changed = conn.execute(update(effects).where(effects.c.session_id == sid,
                effects.c.operation_id == operation_id, effects.c.effect_key == row.effect_key,
                effects.c.disposition == "prepared", effects.c.input_sha256 == row.input_sha256,
                effects.c.receipt_key.is_(None), effects.c.result_chunk_key.is_(None)).values(
                    disposition="refused", receipt_key=receipt_id, result_chunk_key=private_chunk_key(refusal)))
            if changed.rowcount != 1:
                raise ContinuationCASConflict("Stop lost its actual uncommitted SQL/read owner")
            encoded = _effect_snapshot(encoded, effect_key=row.effect_key, disposition="refused", receipt_id=receipt_id)
            encoded = retain_action_execution_receipt_on_connection(conn, session_id=sid,
                operation_id=operation_id, effect_key=row.effect_key, disposition="refused", snapshot_json=encoded)
        return append_complete_snapshot(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor, snapshot_json=encoded,
            event_batch=(ContinuationEvent("effect_refused", '{"phase":"stop_owed_results_joined"}'),), handoff=False)
```

Import the existing effect/job tables and C3/C5 helpers in this authority module. E8's closed `ComposerContinuationStopReady` carries the actual current claim and returned prepared checkpoint/cursor into the terminal owner. It is not a synthetic `ComposerResult`.

- [ ] Add these complete scheduling owner methods to the actual authority. The closed coordinates are written only inside the transaction which actually creates that successor. A new generation's application verifies immutable predecessor history and its actual successor row before returning the tool ACK. No new public message or graph proposal is synthesized.

```python
def commit_continuation_control_successor(
    self, running: ComposerOperationRunning, *, expected_head: UUID | None,
    expected_checkpoint_id: str, expected_cursor: int, next_action_json: str, policy_sha256: str,
) -> ComposerSegmentYieldReceipt:
    from elspeth.web.composer.continuation import EndFrame
    from elspeth.web.composer.continuation_end_values import ContinuationControlHandoff, decode_end_candidate, encode_end_candidate
    from elspeth.web.composer.continuation_execution import continuation_chunk
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    with locked_session_transaction(self._engine, sid) as conn:
        require_continuation_mutation(conn, running.session_operation_context)
        stored = require_snapshot_cas(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        root = decode_continuation_snapshot(stored.payload_json)
        if type(root.frames[-1]) is not EndFrame or root.pending_owner_results or root.pending_settlement_ids:
            raise AuditIntegrityError("control handoff has no settled actual scheduling candidate")
        values = load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=root)
        frame = root.frames[-1]
        candidate = decode_end_candidate(continuation_chunk(values, frame.candidate))
        if candidate.kind != "control" or candidate.control_handoff is not None:
            raise AuditIntegrityError("control handoff is not an unacknowledged actual scheduling request")
        require_retained_provider_application_on_connection(conn, running, stored=stored,
            effect_key=candidate.primary_effect_key, input_sha256=candidate.primary_input_sha256, response=candidate.response)
        claim = running.claim
        if claim.segment_sequence is None or claim.ownership_generation is None:
            raise AuditIntegrityError("control handoff has no actual continuation segment")
        handoff = ContinuationControlHandoff(predecessor_sequence=claim.segment_sequence,
            predecessor_generation=claim.ownership_generation, successor_sequence=claim.segment_sequence + 1,
            successor_generation=claim.ownership_generation + 1)
        chunk = encode_end_candidate(candidate.model_copy(update={"control_handoff": handoff}))
        put_private_chunks(conn, session_id=sid, operation_id=operation_id, values=(chunk,))
        following = root.model_copy(update={"frames": (*root.frames[:-1], frame.model_copy(update={"candidate": chunk.reference}))})
        return yield_segment_on_connection(conn, running, expected_head=expected_head,
            expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
            snapshot_json=encode_continuation_snapshot(following), next_action_json=next_action_json, policy_sha256=policy_sha256)


def commit_continuation_control_application(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int,
    snapshot_json: str, chunks: tuple[PrivateChunk, ...],
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation import ComposeFrame, EndFrame
    from elspeth.web.composer.continuation_codec import decode_continuation_response
    from elspeth.web.composer.continuation_end_values import decode_end_candidate
    from elspeth.web.composer.continuation_execution import continuation_chunk
    from elspeth.web.composer.continuation_message_shelf import decode_message_shelf
    from elspeth.web.composer.provider_thinking import attach_provider_thinking
    from elspeth.web.composer.recovery_controls import parse_continuation_tool_batch
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    with locked_session_transaction(self._engine, sid) as conn:
        require_continuation_mutation(conn, running.session_operation_context, allow_cancel=True, allow_expired_segment=True)
        stored = require_snapshot_cas(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        root, following = decode_continuation_snapshot(stored.payload_json), decode_continuation_snapshot(snapshot_json)
        if (len(root.frames) < 2 or type(root.frames[-1]) is not EndFrame or type(root.frames[-2]) is not ComposeFrame
            or len(following.frames) != len(root.frames)-1 or type(following.frames[-1]) is not ComposeFrame):
            raise AuditIntegrityError("control ACK lost its exact pending END/caller pop")
        values = load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=root)
        candidate = decode_end_candidate(continuation_chunk(values, root.frames[-1].candidate))
        handoff = candidate.control_handoff
        if candidate.kind != "control" or handoff is None:
            raise AuditIntegrityError("control ACK has no actually committed handoff coordinates")
        previous = conn.execute(select(attempts).where(attempts.c.session_id == sid, attempts.c.operation_id == operation_id,
            attempts.c.sequence == handoff.predecessor_sequence, attempts.c.generation == handoff.predecessor_generation)).one()
        successor = conn.execute(select(segments).where(segments.c.session_id == sid, segments.c.operation_id == operation_id,
            segments.c.sequence == handoff.successor_sequence)).one()
        if (previous.outcome != "yielded" or previous.revoked_at is None
            or successor.predecessor_sequence != handoff.predecessor_sequence
            or running.claim.segment_sequence != handoff.successor_sequence
            or running.claim.ownership_generation < handoff.successor_generation):
            raise AuditIntegrityError("control ACK differs from its actual immutable handoff/successor")
        require_retained_provider_application_on_connection(conn, running, stored=stored,
            effect_key=candidate.primary_effect_key, input_sha256=candidate.primary_input_sha256, response=candidate.response)
        completion = decode_continuation_response(continuation_chunk(values, candidate.response))
        control = parse_continuation_tool_batch(completion.tool_batch)
        if control is None:
            raise AuditIntegrityError("control ACK changed its actual admitted sole tool request")
        actual_assistant = attach_provider_thinking({"role": "assistant", "content": completion.message.content,
            "tool_calls": [{"id": call.id, "type": "function", "function": {
                "name": call.function.name, "arguments": call.function.arguments}}
                for call in completion.tool_batch.calls]}, completion.provider_metadata.thinking_blocks)
        from elspeth.contracts.prompt_guard import PROMPT_GUARD_THINKING_PARTS_KEY
        actual_assistant.pop(PROMPT_GUARD_THINKING_PARTS_KEY, None)
        ack = {"role": "tool", "tool_call_id": control.tool_call_id, "content": canonical_json({
            "status": "queued", "operation_id": operation_id, "predecessor_sequence": handoff.predecessor_sequence,
            "successor_sequence": handoff.successor_sequence, "notes": control.notes})}
        old_shelf = decode_message_shelf(continuation_chunk(values, root.frames[-2].transcript))
        new_shelf = decode_message_shelf(continuation_chunk(chunks, following.frames[-1].transcript))
        old_current = tuple(old_shelf.messages_json[index] for index in old_shelf.current_indexes)
        new_current = tuple(new_shelf.messages_json[index] for index in new_shelf.current_indexes)
        if (new_current != (*old_current, canonical_json(actual_assistant), canonical_json(ack))
            or following.frames[-1].phase != "action_ready" or following.cursor != root.cursor
            or following.logical != root.logical or following.policy_hash != root.policy_hash
            or following.admission_policy_json != root.admission_policy_json
            or following.effect_frontier != root.effect_frontier or following.pending_owner_results != root.pending_owner_results
            or following.pending_settlement_ids != root.pending_settlement_ids or following.semantic_frontier != root.semantic_frontier
            or following.active_action_json != root.active_action_json or following.active_action_source != root.active_action_source):
            raise AuditIntegrityError("control ACK changed the actual receipt or another execution owner")
        put_private_chunks(conn, session_id=sid, operation_id=operation_id, values=chunks)
        load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=following)
        return append_complete_snapshot(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor, snapshot_json=snapshot_json,
            event_batch=(ContinuationEvent("review_retained", canonical_json({"kind": "control_ack", "tool_call_id": control.tool_call_id})),), handoff=False)
```

- [ ] Add the full pure result application below to `continuation_end.py`. Its tool response reports the actual queued logical operation; it does not report semantic completion or any unexecuted graph effect. The selected current authority rechecks all coordinates and exact two-message extension before committing the pop, including when Stop arrives after the already committed handoff.

```python
async def apply_continuation_control_receipt(
    *, service: ComposerServiceImpl, operation_authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState, required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner, progress: ComposerProgressSink | None,
) -> SelectedContinuationState:
    from elspeth.web.composer.continuation_execution import capture_continuation_runtime_owners
    from elspeth.web.composer.provider_thinking import attach_provider_thinking
    from elspeth.web.composer.recovery_controls import parse_continuation_tool_batch
    end, candidate = selected_end_candidate(selected)
    handoff = candidate.control_handoff
    if candidate.kind != "control" or handoff is None:
        raise AuditIntegrityError("control application has no retained actual queued receipt")
    runtime = await restore_continuation_runtime(service=service, operation_authority=operation_authority,
        checkpoint_id=selected.checkpoint_id, stack=selected.stack, chunks=selected.chunks,
        required_work=required_work, provider_owner=provider_owner, progress=progress)
    completion = decode_continuation_response(continuation_chunk(selected.chunks, candidate.response))
    control = parse_continuation_tool_batch(completion.tool_batch)
    if control is None:
        raise AuditIntegrityError("control receipt lost its actual admitted request")
    runtime.live.llm_messages.append(attach_provider_thinking({"role": "assistant", "content": completion.message.content,
        "tool_calls": [{"id": call.id, "type": "function", "function": {
            "name": call.function.name, "arguments": call.function.arguments}} for call in completion.tool_batch.calls]},
        completion.provider_metadata.thinking_blocks))
    runtime.live.llm_messages.append({"role": "tool", "tool_call_id": control.tool_call_id, "content": canonical_json({
        "status": "queued", "operation_id": selected.stack.logical.operation_id,
        "predecessor_sequence": handoff.predecessor_sequence, "successor_sequence": handoff.successor_sequence,
        "notes": control.notes})})
    runtime.live.turn_evidence.transcript.observe_messages(runtime.live.llm_messages)
    stack, values = capture_continuation_runtime_owners(runtime=runtime, stack=selected.stack, chunks=selected.chunks)
    stack = stack.model_copy(update={"frames": stack.frames[:-1]})
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL,
        RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(sql, operation_authority.commit_continuation_control_application,
            _end_running(required_work), expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.stack.cursor,
            snapshot_json=encode_continuation_snapshot(stack), chunks=values)
    except BaseException:
        projection.complete_without_submission()
        raise
    stored, cancellations = _end_sql_result(required_work=required_work, sql=sql, projection=projection,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL, outcome=outcome)
    return await _end_selected(authority=operation_authority, required_work=required_work,
        stored=stored, cancellations=cancellations)
```

The C4 runner's existing ordinary safe-boundary yield branch must select the concrete SQL method before submitting its actual required SQL future. The exact added selector is below; preserve its actual registered outcome projection, durable receipt return and worker `verify_yield` readback. For the control method omit `snapshot_json`, because that owner derives its complete root inside its held transaction. Both branches pass the same real expected graph head, selected checkpoint/cursor, retained next-action JSON and policy hash.

```python-statement
control_candidate = None
if type(advanced.stack.frames[-1]) is EndFrame:
    control_candidate = decode_end_candidate(continuation_chunk(advanced.chunks, advanced.stack.frames[-1].candidate))
control_handoff_requested = (control_candidate is not None and control_candidate.kind == "control"
    and control_candidate.control_handoff is None)
if control_handoff_requested:
    outcome = await run_required_sql_finish_once(sql, authority.commit_continuation_control_successor,
        running, expected_head=expected_head, expected_checkpoint_id=advanced.checkpoint_id,
        expected_cursor=advanced.stack.cursor, next_action_json=next_action.model_dump_json(),
        policy_sha256=advanced.stack.policy_hash)
else:
    outcome = await run_required_sql_finish_once(sql, authority.commit_checkpoint_and_successor,
        running, expected_head=expected_head, expected_checkpoint_id=advanced.checkpoint_id,
        expected_cursor=advanced.stack.cursor, snapshot_json=encode_continuation_snapshot(advanced.stack),
        next_action_json=next_action.model_dump_json(), policy_sha256=advanced.stack.policy_hash)
```

Use `authority.commit_continuation_control_successor` for `control_handoff_requested` and the existing `authority.commit_checkpoint_and_successor` otherwise. This is a closed choice between two actual SQL owners; an uncommitted `ContinuationAdvance(result=None)` is never a queued ACK. The runner returns `ContinuationSegmentYielded` only after its actual `ComposerSegmentYieldReceipt` has been projected.

- [ ] Add this complete SQL read facet and insert its exact registered read before `finalize_continuation_end` restores runtime. Final publication is checked again inside the actual terminal transaction. The first read prevents deterministic terminal writers from running on a partially applied semantic decision.

```python
def validate_continuation_terminal(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int,
) -> bool:
    sid = str(running.claim.session_id)
    with locked_session_transaction(self._engine, sid) as conn:
        require_continuation_mutation(conn, running.session_operation_context)
        stored = require_snapshot_cas(conn, session_id=sid, operation_id=running.claim.operation_id,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        require_continuation_terminal_on_connection(conn, running, stored=stored)
        return True
```

The inserted async read block is complete inside the named async finalizer:

```python-statement
sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL,
    RequiredWorkSource.CONTINUATION_READ_PROJECTION)
try:
    outcome = await run_required_sql_finish_once(sql, operation_authority.validate_continuation_terminal,
        _end_running(required_work), expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.stack.cursor)
except BaseException:
    projection.complete_without_submission()
    raise
authorized, cancellations = _end_sql_result(required_work=required_work, sql=sql, projection=projection,
    source=RequiredWorkSource.CONTINUATION_READ_SQL, outcome=outcome, projection_kind="authorization")
if authorized is not True:
    raise AuditIntegrityError("terminal read returned no actual selected authorization")
_end_deliver_cancellations(cancellations)
```

- [ ] Replace E0's signature-only declaration with the complete outward dispatcher below. It advances one actual phase at a time. The only nonterminal outward `ContinuationAdvance(None)` is the actual model's explicit scheduling request; the runner still must commit and verify its real successor. Ordinary review/semantic phases return a complete selected checkpoint for the existing dispatcher to fit the next action. A durable Stop starts no new provider, handler, source inspection or validation run.

```python
async def advance_continuation_end(
    *, service: ComposerServiceImpl, operation_authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState, required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner, progress: ComposerProgressSink | None,
) -> SelectedContinuationState | ContinuationAdvance:
    from elspeth.web.composer.advisor_output import ReviewContinuationAction
    from elspeth.web.composer.continuation_driver import run_continuation_advisor_provider
    from elspeth.web.composer.continuation import ContinuationAdvance
    from elspeth.web.composer.continuation_execution import read_continuation_boundary
    from elspeth.web.composer.continuation_progress import decode_prescribed_continuation_action
    from elspeth.web.sessions.composer_continuation_stop import ComposerContinuationStopReady
    if required_work.running is None:
        raise AuditIntegrityError("END lost its actual running/required-work owner")
    boundary = await read_continuation_boundary(authority=operation_authority,
        selected=selected, required_work=required_work)
    top = selected.stack.frames[-1]
    if type(top) is AdvisorFrame:
        source = decode_continuation_advisor_invocation(continuation_chunk(selected.chunks, top.review))
        if source.kind not in ("publication", "semantic"):
            raise AuditIntegrityError("END cannot apply a manual or EARLY child")
        if top.phase == "response_retained" and source.kind == "publication":
            return await apply_continuation_publication_review(service=service, operation_authority=operation_authority,
                selected=selected, required_work=required_work, provider_owner=provider_owner, progress=progress)
    if boundary.cancel_requested:
        sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL,
            RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
        try:
            outcome = await run_required_sql_finish_once(sql, operation_authority.prepare_continuation_stop,
                _end_running(required_work), expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.stack.cursor)
        except BaseException:
            projection.complete_without_submission()
            raise
        stored, cancellations = _end_sql_result(required_work=required_work, sql=sql, projection=projection,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL, outcome=outcome)
        await _end_selected(authority=operation_authority, required_work=required_work, stored=stored)
        cancelled = ComposerContinuationStopReady(claim=_end_running(required_work).claim,
            checkpoint_id=stored.checkpoint_id, cursor=stored.cursor)
        if cancellations:
            raise BaseExceptionGroup("Durable Stop and actual deferred cancellations", (cancelled, *cancellations))
        raise cancelled
    if type(top) is ComposeFrame and top.phase == "response_retained":
        return await prepare_continuation_end_candidate(service=service, operation_authority=operation_authority,
            selected=selected, required_work=required_work, provider_owner=provider_owner, progress=progress)
    if type(top) is AdvisorFrame:
        if top.phase != "response_retained":
            return await run_continuation_advisor_provider(service=service, operation_authority=operation_authority,
                selected=selected, required_work=required_work, provider_owner=provider_owner, progress=progress)
        return await apply_continuation_semantic_review(service=service, operation_authority=operation_authority,
            selected=selected, required_work=required_work, provider_owner=provider_owner, progress=progress)
    if type(top) is not EndFrame:
        raise AuditIntegrityError("END selected no received completion, candidate or automatic review")
    end, candidate = selected_end_candidate(selected)
    if candidate.kind == "control":
        if candidate.control_handoff is None:
            if not selected.stack.is_handoff_safe:
                raise AuditIntegrityError("explicit control reached an unsettled handoff")
            return ContinuationAdvance(selected.checkpoint_id, selected.stack, selected.chunks, None)
        return await apply_continuation_control_receipt(service=service, operation_authority=operation_authority,
            selected=selected, required_work=required_work, provider_owner=provider_owner, progress=progress)
    if selected.stack.active_action_json is not None:
        action = decode_prescribed_continuation_action(selected.stack.active_action_json)
        if type(action) is not ReviewContinuationAction:
            raise AuditIntegrityError("END cannot dispatch a prescribed primary/read owner")
        return await push_continuation_end_review(service=service, operation_authority=operation_authority,
            selected=selected, required_work=required_work, provider_owner=provider_owner, progress=progress, semantic=False)
    if candidate.publication_source is None:
        return await push_continuation_end_review(service=service, operation_authority=operation_authority,
            selected=selected, required_work=required_work, provider_owner=provider_owner, progress=progress, semantic=False)
    if candidate.semantic_source is None:
        return await push_continuation_end_review(service=service, operation_authority=operation_authority,
            selected=selected, required_work=required_work, provider_owner=provider_owner, progress=progress, semantic=True)
    return await finalize_continuation_end(service=service, operation_authority=operation_authority,
        selected=selected, required_work=required_work, provider_owner=provider_owner, progress=progress)
```

The frames dispatcher owns action fit before new automatic review/END work. Received response and private-result application are owed stages and may finish with Stop/segment expiry; they never dispatch a new action. C4 retains its actual request lifecycle, watcher, current lease, child joins and verified terminal/yield readback. Parent/worker cancellation settlement remains the existing typed failure owner.

## E6: Prove actual pass rollback, cold application and current terminal admission

**Test:** Extend `tests/unit/web/composer/test_continuation_end.py` after the complete D1–D3 helper module exists. Import its real SQLite operation-store fixture, actual SDK boundary fake and actual worker environment; do not replace the runner, phase driver, SQL owner, final result or effect oracle. These are future execution steps. No test was run while writing this plan.

- [ ] Add these exact real-store tests. The rollback test injects after the real audit insert and verifies every current metadata table except the independently renewed session fence, plus the complete selected snapshot and pass rows before the worker can append a recovery event. Its second actual worker consumes the retained received child, so the same Advisor request cannot be sent again. The forged-record variant is importable and reaches the SQL owner's behavioral assertion.

```python
import asyncio
import json
import sqlite3
from dataclasses import replace

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.advisor_audit import AdvisorCheckpointPassRecord, advisor_checkpoint_pass_audit_envelope
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.models import (
    chat_messages_table, composer_continuation_effects_table, metadata, session_operation_fences_table,
)
from elspeth.web.sessions.service import SessionServiceImpl
from tests.helpers.continuation_driver import (
    DeterministicDriverSDK, DriverCutObservation, assert_same_driver_outcome,
    clone_admitted_database, compare_driver_cut, fresh_driver_environment,
    ordinary_driver_script, read_driver_outcome, recover_copied_driver_owner,
    run_driver_attempt, seed_driver_case, select_driver_state,
)
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as operation_store


def complete_store_rows(engine):
    with engine.connect() as conn, conn.begin():
        return tuple((table.name, tuple(conn.execute(select(table).order_by(*table.primary_key.columns)).all()))
                     for table in metadata.sorted_tables if table is not session_operation_fences_table)


def actual_end_pass_rows(engine, session_id):
    with engine.connect() as conn:
        rows = conn.execute(select(chat_messages_table).where(
            chat_messages_table.c.session_id == str(session_id), chat_messages_table.c.role == "audit",
        ).order_by(chat_messages_table.c.sequence_no)).all()
    result = []
    for row in rows:
        if type(row.tool_calls) is not list or len(row.tool_calls) != 1:
            continue
        envelope = row.tool_calls[0]
        if type(envelope) is not dict or "pass" not in envelope:
            continue
        record = AdvisorCheckpointPassRecord.from_dict(envelope["pass"])
        if record.phase == "end":
            assert envelope == advisor_checkpoint_pass_audit_envelope(record)
            assert row.content == json.dumps(envelope, sort_keys=True)
            result.append((row.id, record))
    return tuple(result)


async def actual_test_continuation_owner(env):
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning
    from tests.helpers.continuation_policy import minimal_continuation_policy
    deadline = asyncio.get_running_loop().time() + 10
    while True:
        claims = env.authority.claim_segment(limit=1)
        if claims:
            assert len(claims) == 1
            claim = claims[0]
            context = env.sessions.session_operation_authority.start_composer_segment(claim,
                owner_instance_id="test-owner", lease_seconds=30, auth_provider_type="local",
                segment_policy=minimal_continuation_policy().segment)
            return ComposerOperationRunning(claim=claim, session_operation_context=context)
        assert asyncio.get_running_loop().time() < deadline, "actual SQL segment eligibility did not become available"
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_end_received_child_cold_resume_preserves_one_actual_pass_and_full_outcome(operation_store, tmp_path, monkeypatch):
    compared = await compare_driver_cut(operation_store, tmp_path, monkeypatch, "end_response")
    assert_same_driver_outcome(compared.baseline, compared.resumed)
    env = fresh_driver_environment(compared.cold_database_url, compared.case)
    try:
        passes = actual_end_pass_rows(env.sessions._engine, compared.case.session_id)
        assert len(passes) == 1
        assert passes[0][1].provider_attempts == 1 and not passes[0][1].format_reprompt_sent
        assert sum(call.role == "end" for call in compared.resumed_sdk.invocations) == 1
        assert sum(call.role == "semantic" for call in compared.resumed_sdk.invocations) == 1
    finally:
        env.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("after_real_insert", "forged_offered_count", "forged_phase"))
async def test_actual_pass_audit_and_checkpoint_roll_back_then_cold_apply_once(
    operation_store, tmp_path, monkeypatch, fault,
):
    case = await seed_driver_case(operation_store, tmp_path / "data")
    baseline_url = clone_admitted_database(case, tmp_path / "baseline.db")
    env = fresh_driver_environment(baseline_url, case)
    sdk = ordinary_driver_script(case)
    original_sync = SessionServiceImpl._persist_continuation_advisor_pass_sync
    original_insert = SessionServiceImpl._insert_chat_message
    observations = []
    injected = RuntimeError("controlled interruption after the actual pass insert")
    armed = {"used": False, "envelope": None}

    def actual_insert(self, *args, **kwargs):
        result = original_insert(self, *args, **kwargs)
        if fault == "after_real_insert" and armed["envelope"] is not None and kwargs.get("tool_calls") == [armed["envelope"]]:
            raise injected
        return result

    def guarded_sync(self, **kwargs):
        record = kwargs["record"]
        if record.phase != "end" or armed["used"]:
            return original_sync(self, **kwargs)
        armed["used"] = True
        before = complete_store_rows(self._engine)
        with self._engine.connect() as conn:
            head = load_snapshot(conn, session_id=str(case.session_id), operation_id=case.operation_id)
        armed["envelope"] = advisor_checkpoint_pass_audit_envelope(record)
        if fault == "forged_offered_count":
            assert record.step_ids_offered is not None
            kwargs["record"] = replace(record, step_ids_offered=record.step_ids_offered + 1)
        elif fault == "forged_phase":
            kwargs["record"] = replace(record, phase="early", pass_index=0)
        try:
            return original_sync(self, **kwargs)
        except BaseException as error:
            if fault == "after_real_insert":
                assert error is injected
            else:
                assert isinstance(error, AuditIntegrityError)
            assert complete_store_rows(self._engine) == before
            with self._engine.connect() as conn:
                assert load_snapshot(conn, session_id=str(case.session_id), operation_id=case.operation_id) == head
            observations.append(head)
            raise
        finally:
            armed["envelope"] = None

    try:
        with monkeypatch.context() as patch:
            patch.setattr("litellm.acompletion", sdk.complete)
            patch.setattr(SessionServiceImpl, "_insert_chat_message", actual_insert)
            patch.setattr(SessionServiceImpl, "_persist_continuation_advisor_pass_sync", guarded_sync)
            await run_driver_attempt(env, sdk, patch, expect_suspension=True)
        assert len(observations) == 1 and actual_end_pass_rows(env.sessions._engine, case.session_id) == ()
        env.dispose()
        # Discard all service/runtime/lease/coordinator/transcript objects.
        env = fresh_driver_environment(baseline_url, case)
        with monkeypatch.context() as patch:
            patch.setattr("litellm.acompletion", sdk.complete)
            await run_driver_attempt(env, sdk, patch)
        sdk.assert_consumed()
        passes = actual_end_pass_rows(env.sessions._engine, case.session_id)
        assert len(passes) == 1 and passes[0][1].phase == "end"
        assert sum(call.role == "end" for call in sdk.invocations) == 1
        assert sum(call.role == "semantic" for call in sdk.invocations) == 1
        outcome = read_driver_outcome(env, sdk)
        assert "Final exact café description" in outcome.graphs_json
    finally:
        env.dispose()


@pytest.mark.asyncio
async def test_current_terminal_owner_rejects_a_received_but_unapplied_end_child(operation_store, tmp_path, monkeypatch):
    from elspeth.web.coordination import composer_operation_authority as authority_module
    case = await seed_driver_case(operation_store, tmp_path / "data")
    database = clone_admitted_database(case, tmp_path / "baseline.db")
    cut = DriverCutObservation("end_response", copy_to=tmp_path / "unapplied.db")
    env = fresh_driver_environment(database, case)
    sdk = ordinary_driver_script(case)
    actual_guard = authority_module.require_continuation_terminal_on_connection
    allowed = []
    def terminal_guard(conn, running, *, stored):
        result = actual_guard(conn, running, stored=stored)
        allowed.append(stored.checkpoint_id)
        return result
    try:
        with monkeypatch.context() as patch:
            patch.setattr("litellm.acompletion", sdk.complete)
            patch.setattr(authority_module, "require_continuation_terminal_on_connection", terminal_guard)
            await run_driver_attempt(env, sdk, patch, cut=cut, interrupt=False)
        assert allowed, "The positive control must reach actual terminal admission"
        sdk.assert_consumed()
    finally:
        env.dispose()
    cold = fresh_driver_environment(f"sqlite:///{tmp_path / 'unapplied.db'}", case)
    running = None
    try:
        assert cut.selected is not None and select_driver_state(cold) == cut.selected
        recover_copied_driver_owner(cold)
        running = await actual_test_continuation_owner(cold)
        stored = cold.authority.load_continuation_snapshot(running)
        before = complete_store_rows(cold.sessions._engine)
        with pytest.raises(AuditIntegrityError, match="publication|logical success"):
            cold.authority.validate_continuation_terminal(running,
                expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor)
        assert complete_store_rows(cold.sessions._engine) == before
        assert cold.authority.load_continuation_snapshot(running) == stored
    finally:
        if running is not None:
            cold.sessions.session_operation_authority.release(running.session_operation_context)
        cold.dispose()
```

The authority module imports the terminal helper from `sessions.continuation`; the positive-control spy wraps that actual imported symbol. The tests do not infer a pass from SDK call count: they decode the actual persisted audit envelope, inspect complete SQL/store state and use D's normalized full conversation/graph/proposal/interpretation/evidence/action/accounting oracle. The actual independently renewed session-fence table is excluded from the transaction byte comparison; its existing owner/current-fence assertions remain in D/W. Every other actual metadata table is compared, including business rows, private chunks, checkpoints/events, logical job/segment and physical accounting.

- [ ] Add this exact old-prepared-read cold test to the same module. Its copied checkpoint is taken after the actual effect preparation transaction commits and before the current handler runs. The original preparing lineage stays unchanged; the new current committing generation must be retained separately. A modified prepared-input hash is the negative control. The local SQLite backup is the same explicit cut mechanism as D, not a fabricated final state.

```python
@pytest.mark.asyncio
async def test_old_prepared_immutable_read_uses_current_committer_without_reminting_preparation(
    operation_store, tmp_path, monkeypatch,
):
    case = await seed_driver_case(operation_store, tmp_path / "data")
    database = clone_admitted_database(case, tmp_path / "baseline.db")
    cold_path = tmp_path / "prepared-read.db"
    env = fresh_driver_environment(database, case)
    sdk = ordinary_driver_script(case)
    original_prepare = env.authority.prepare_tool_action
    copied = []
    def prepare(running, **kwargs):
        stored = original_prepare(running, **kwargs)
        if kwargs["owner"] == "read" and not copied:
            with sqlite3.connect(database.removeprefix("sqlite:///")) as source:
                with sqlite3.connect(str(cold_path)) as target:
                    source.backup(target)
            copied.append((select_driver_state(env), tuple(sdk.invocations), dict(sdk.consumed), kwargs["effect_key"],
                           running.claim.segment_sequence, running.claim.ownership_generation))
        return stored
    try:
        with monkeypatch.context() as patch:
            patch.setattr("litellm.acompletion", sdk.complete)
            patch.setattr(env.authority, "prepare_tool_action", prepare)
            await run_driver_attempt(env, sdk, patch)
        assert len(copied) == 1 and cold_path.exists()
        baseline = read_driver_outcome(env, sdk)
    finally:
        env.dispose()
    selected, prefix, consumed, effect_key, original_sequence, original_generation = copied[0]
    cold = fresh_driver_environment(f"sqlite:///{cold_path}", case)
    resumed_sdk = DeterministicDriverSDK(replies=ordinary_driver_script(case).replies,
        consumed=consumed, invocations=list(prefix))
    try:
        assert select_driver_state(cold) == selected
        recover_copied_driver_owner(cold)
        from pydantic import TypeAdapter
        from elspeth.contracts.composer_audit import PrivateToolAuthoring
        from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
        from elspeth.web.composer.continuation_execution import continuation_chunk
        from elspeth.web.composer.continuation_tool_batch import decode_batch_chunk, restore_dispatch_audit
        from elspeth.web.composer.continuation_tool_receipts import ContinuationToolCompletion, ToolCompletionValue
        from elspeth.web.sessions.continuation import ContinuationCASConflict, ContinuationOwnerCommit, load_private_chunks, require_owner_commit
        guarded = await actual_test_continuation_owner(cold)
        try:
            with cold.sessions._engine.connect() as conn:
                stored = load_snapshot(conn, session_id=str(case.session_id), operation_id=case.operation_id)
                root = decode_continuation_snapshot(stored.payload_json)
                values = load_private_chunks(conn, session_id=str(case.session_id), operation_id=case.operation_id, stack=root)
                batch = decode_batch_chunk(continuation_chunk(values, root.frames[-1].accumulator))
                effect = conn.execute(select(composer_continuation_effects_table).where(
                    composer_continuation_effects_table.c.session_id == str(case.session_id),
                    composer_continuation_effects_table.c.operation_id == case.operation_id,
                    composer_continuation_effects_table.c.effect_key == effect_key)).one()
            call = batch.current_call
            assert call is not None and call.completion_json is not None
            authoring = tuple(item for item in TypeAdapter(tuple[PrivateToolAuthoring, ...]).validate_json(batch.private_authorings_json)
                              if item.tool_call_id == call.call_id)
            assert len(authoring) == 1
            completion = ContinuationToolCompletion(ToolCompletionValue.model_validate_json(call.completion_json),
                restore_dispatch_audit(call.audit), authoring[0])
            commit = ContinuationOwnerCommit(guarded, effect_key, effect.input_sha256,
                stored.checkpoint_id, stored.cursor, completion)
            before = complete_store_rows(cold.sessions._engine)
            with cold.sessions._engine.begin() as conn:
                assert require_owner_commit(conn, commit, guarded.session_operation_context, allow_completed_read=True) == stored
                with pytest.raises(ContinuationCASConflict):
                    require_owner_commit(conn, replace(commit, input_sha256="0" * 64),
                        guarded.session_operation_context, allow_completed_read=True)
            assert complete_store_rows(cold.sessions._engine) == before
            receipt = cold.authority.suspend_segment(guarded, retry_delay_seconds=1, reason="owner_unavailable")
            cold.authority.verify_recovery(receipt)
        finally:
            cold.sessions.session_operation_authority.release(guarded.session_operation_context)
        with monkeypatch.context() as patch:
            patch.setattr("litellm.acompletion", resumed_sdk.complete)
            await run_driver_attempt(cold, resumed_sdk, patch)
        resumed_sdk.assert_consumed()
        assert_same_driver_outcome(baseline, read_driver_outcome(cold, resumed_sdk))
        with cold.sessions._engine.connect() as conn:
            effect = conn.execute(select(composer_continuation_effects_table).where(
                composer_continuation_effects_table.c.session_id == str(case.session_id),
                composer_continuation_effects_table.c.operation_id == case.operation_id,
                composer_continuation_effects_table.c.effect_key == effect_key,
            )).one()
        assert (effect.creator_sequence, effect.creator_generation) == (original_sequence, original_generation)
        assert effect.committer_sequence == original_sequence and effect.committer_generation > original_generation
        assert effect.disposition == "committed" and effect.receipt_key == effect_key
    finally:
        cold.dispose()
```

- [ ] Add this exact actual-control handoff/replay test. It pauses no fake runner: the first real worker must return a verified durable successor while the parent is nonterminal. The ACK cut is after the actual application SQL transaction; a new process-local owner must enter the next model turn without repeating either the scheduling request or its ACK. The full original operation and remaining obligation survive.

```python
@pytest.mark.asyncio
async def test_control_ack_follows_real_successor_and_is_applied_once_after_cold_cut(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_end_values import decode_end_candidate
    from elspeth.web.composer.continuation_execution import continuation_chunk
    from elspeth.web.sessions.continuation import load_private_chunks
    from tests.helpers.continuation_driver import SDKReply, tool_reply
    case = await seed_driver_case(operation_store, tmp_path / "data")
    database = clone_admitted_database(case, tmp_path / "control.db")
    ordinary = ordinary_driver_script(case)
    replies = dict(ordinary.replies)
    replies["primary"] = (*replies["primary"][:-1], SDKReply(calls=(tool_reply(
        "schedule:one", "checkpoint_and_continue", {"notes": "Retain the full original request and saved description."}),)), replies["primary"][-1])
    sdk = DeterministicDriverSDK(replies=replies)
    env = fresh_driver_environment(database, case)
    ack_path = tmp_path / "ack-committed.db"
    ack_cut = []
    try:
        with monkeypatch.context() as patch:
            patch.setattr("litellm.acompletion", sdk.complete)
            first = await run_driver_attempt(env, sdk, patch)
        parent = env.authority.get(session_id=case.session_id, operation_id=case.operation_id)
        assert parent.status == "running" and parent.current_segment_sequence == first.sequence + 1
        selected = select_driver_state(env)
        with env.sessions._engine.connect() as conn:
            stored = load_snapshot(conn, session_id=str(case.session_id), operation_id=case.operation_id)
            from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
            root = decode_continuation_snapshot(stored.payload_json)
            chunks = load_private_chunks(conn, session_id=str(case.session_id), operation_id=case.operation_id, stack=root)
        candidate = decode_end_candidate(continuation_chunk(chunks, root.frames[-1].candidate))
        assert candidate.kind == "control" and candidate.control_handoff is not None
        assert candidate.control_handoff.successor_sequence == parent.current_segment_sequence
        assert sum(item.role == "semantic" for item in sdk.invocations) == 0
        actual_apply = env.authority.commit_continuation_control_application
        def apply(running, **kwargs):
            committed = actual_apply(running, **kwargs)
            if not ack_cut:
                with sqlite3.connect(database.removeprefix("sqlite:///")) as source:
                    with sqlite3.connect(str(ack_path)) as target:
                        source.backup(target)
                ack_cut.append((select_driver_state(env), tuple(sdk.invocations), dict(sdk.consumed)))
            return committed
        with monkeypatch.context() as patch:
            patch.setattr("litellm.acompletion", sdk.complete)
            patch.setattr(env.authority, "commit_continuation_control_application", apply)
            await run_driver_attempt(env, sdk, patch)
        sdk.assert_consumed()
        baseline = read_driver_outcome(env, sdk)
        assert len(ack_cut) == 1 and selected.parent_status == "running"
    finally:
        env.dispose()
    selected_ack, prefix, consumed = ack_cut[0]
    cold = fresh_driver_environment(f"sqlite:///{ack_path}", case)
    resumed = DeterministicDriverSDK(replies=replies, consumed=consumed, invocations=list(prefix))
    try:
        assert select_driver_state(cold) == selected_ack
        recover_copied_driver_owner(cold)
        with monkeypatch.context() as patch:
            patch.setattr("litellm.acompletion", resumed.complete)
            await run_driver_attempt(cold, resumed, patch)
        resumed.assert_consumed()
        assert_same_driver_outcome(baseline, read_driver_outcome(cold, resumed))
        # Compare the actual next full safe SDK request, not a role counter.
        assert resumed.invocations[len(prefix)] == sdk.invocations[len(prefix)]
        with cold.sessions._engine.connect() as conn:
            stored = load_snapshot(conn, session_id=str(case.session_id), operation_id=case.operation_id)
            root = decode_continuation_snapshot(stored.payload_json)
            chunks = load_private_chunks(conn, session_id=str(case.session_id), operation_id=case.operation_id, stack=root)
        compose = tuple(frame for frame in root.frames if frame.frame_kind == "compose")
        assert len(compose) == 1
        from elspeth.web.composer.continuation_message_shelf import decode_message_shelf
        shelf = decode_message_shelf(continuation_chunk(chunks, compose[0].transcript))
        messages = [json.loads(shelf.messages_json[index]) for index in shelf.current_indexes]
        acknowledgements = [message for message in messages if message.get("role") == "tool" and message.get("tool_call_id") == "schedule:one"]
        assert len(acknowledgements) == 1 and json.loads(acknowledgements[0]["content"])["status"] == "queued"
    finally:
        cold.dispose()
```

- [ ] Establish behavioral RED on this importable packet: move only C5.9's real `_insert_chat_message` outside its transaction, require the `complete_store_rows == before` assertion to fail, then restore it. Remove only the terminal whole-Stop guard and require the current-owner refusal test to fail. Reintroduce the old read creator-equality refusal and require the old-prepared-read test to fail at the actual owner result retention. Move the ACK before actual successor commit and require the control test's actual queued coordinates/once-only ACK assertions to fail. Restore all changes. Missing modules, import/collection errors and fixture/configuration failures are prerequisite defects, never RED evidence.

- [ ] Run the pure/cold/actual-worker packet with native exit zero, then the existing persistence and whole-tree gates from main Q. PostgreSQL execution uses the same actual store tests through its real fixture; SQLite success does not certify PostgreSQL transaction or reflected CHECK behavior.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_continuation_end.py tests/unit/web/composer/test_continuation_driver.py tests/unit/web/sessions/test_continuation_cold_restore.py tests/unit/web/composer/test_pending_review_advisor_terminal.py tests/unit/web/composer/test_graph_recovery.py >"$COMPOSER_LOG_DIR/continuation-end.log" 2>&1
COMPOSER_END_EXIT=$?
printf 'exit=%s\n' "$COMPOSER_END_EXIT"
cat "$COMPOSER_LOG_DIR/continuation-end.log"
test "$COMPOSER_END_EXIT" -eq 0
```

- [ ] After this packet and its dependencies pass, review the exact source/test paths, run `git diff --check`, the ordinary branch-safety check and hooks, and commit as `feat(composer): apply retained END decisions before terminal publication`. Stage only the new END modules, the named existing authority/service/continuation/protocol owners and this test. Do not stage private lane notes, logs or another owner's incomplete dependency.

```bash
cd "$COMPOSER_WORKTREE" && git status --short
cd "$COMPOSER_WORKTREE" && git diff --check
cd "$COMPOSER_WORKTREE" && git add src/elspeth/web/composer/continuation_end.py src/elspeth/web/composer/continuation_end_values.py src/elspeth/web/composer/continuation_semantic_bindings.py src/elspeth/web/composer/continuation_progress.py src/elspeth/web/composer/advisor_output.py src/elspeth/web/sessions/continuation.py src/elspeth/web/coordination/composer_operation_authority.py src/elspeth/web/sessions/service.py src/elspeth/web/sessions/protocol.py tests/unit/web/composer/test_continuation_end.py tests/unit/web/composer/test_continuation_progress.py
cd "$COMPOSER_WORKTREE" && git diff --cached --stat
cd "$COMPOSER_WORKTREE" && scripts/branch-safety-check.sh --intent commit
cd "$COMPOSER_WORKTREE" && git commit -m "feat(composer): apply retained END decisions before terminal publication"
```

The pure schema/codec prerequisites may land in the earlier disabled prerequisite commits specified by the main DAG; do not repeat an already committed path change merely because it appears in this final packet's source list. The C4 runner branch belongs with its normal runner source commit. Hooks remain enabled and every native check must finish before its result is reported.

## E7: Enforce the actual non-provider END phase clocks

**Modify:** `composer/continuation_end.py`; extend `test_continuation_end.py`.
Apply E7's source extraction before running E6. Action fit is admission only; it is not the timer below. Provider execution remains exclusively inside C5/F21's own independent stages. These local clocks cover actual deterministic END work and owned SQL result retention, and never claim that a canceled SQL waiter killed its actual producer.

- [ ] Add these complete imports and clock selector. It uses the complete request-retained policy, including after new admissions are disabled. It cannot substitute current defaults. The existing `ContinuationActionClock.run` owns original timeout/cancellation propagation; the SQL finish-once owner still projects or retains custody of its real submitted work.

```python
from typing import Never

from elspeth.web.composer.advisor_audit import AdvisorCheckpointPassRecord
from elspeth.web.composer.continuation import ContinuationFrameStack
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_policy import decode_continuation_policy


def _end_phase_clock(selected: SelectedContinuationState) -> ContinuationActionClock:
    policy = decode_continuation_policy(selected.stack.admission_policy_json, selected.stack.policy_hash)
    clock = ContinuationActionClock(policy.actions.session_sql)
    clock.finish_preparation()
    return clock
```

- [ ] Extract E2's exact already-literal `prepare_continuation_end_candidate` prefix into `_prepare_continuation_end_value`, preserving its original six keyword parameters and annotations. The retained prefix starts at its first local import and ends at `values = merge_continuation_chunks(values, (chunk,))`, inclusive. It includes all actual primary restoration, deterministic validation/source inspection, full feedback and private owner encoding; move every line unchanged. Its return annotation is `tuple[ContinuationFrameStack, tuple[PrivateChunk, ...]]`; append exactly `return following, values`. Its original suffix, beginning `sql, projection = required_work.reserve_pair` and ending the function's `_end_selected` return, becomes `_retain_continuation_end_value` with the same six keyword parameters plus `following:ContinuationFrameStack` and `values:tuple[PrivateChunk,...]`. Move every line unchanged and keep return annotation `SelectedContinuationState`. Import `ContinuationFrameStack` normally in this runtime module. These are concrete extractions of the complete E2 body, not new callback ports.

The exact public replacement is:

```python
async def prepare_continuation_end_candidate(
    *, service: ComposerServiceImpl, operation_authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState, required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner, progress: ComposerProgressSink | None,
) -> SelectedContinuationState:
    clock = _end_phase_clock(selected)
    async def prepare() -> tuple[ContinuationFrameStack, tuple[PrivateChunk, ...]]:
        return await _prepare_continuation_end_value(service=service, operation_authority=operation_authority,
            selected=selected, required_work=required_work, provider_owner=provider_owner, progress=progress)
    following, values = await clock.run("physical", prepare)
    async def retain() -> SelectedContinuationState:
        return await _retain_continuation_end_value(service=service, operation_authority=operation_authority,
            selected=selected, required_work=required_work, provider_owner=provider_owner, progress=progress,
            following=following, values=values)
    return await clock.run("retention", retain)
```

- [ ] Apply the same exact two-span extraction to the complete E3 received-publication body: `_prepare_continuation_publication_value` has the original six keyword parameters, preserves the body from its first local import through `values = merge_continuation_chunks(values, (candidate_chunk,))`, and appends `return stack, values, child, record`. Its explicit return type is `tuple[ContinuationFrameStack, tuple[PrivateChunk,...], AdvisorFrame, AdvisorCheckpointPassRecord]`; import the actual nominal record normally. `_retain_continuation_publication_value` preserves the entire remaining SQL/projection/return suffix and takes those four exact returned values plus the original six keywords. Its actual `persist_continuation_advisor_pass_finish_once` call, record, child identity and deferred cancellation handling stay unchanged.

Prepend this exact guard to that retained suffix. The child's paired source was checked in the preparation helper; the retention helper independently narrows the complete value it receives before submitting SQL.

```python-statement
if child.provider_effect_key is None or child.provider_input_sha256 is None or child.response is None:
    raise AuditIntegrityError("publication retention lost its exact received physical source")
```

The public replacement is complete:

```python
async def apply_continuation_publication_review(
    *, service: ComposerServiceImpl, operation_authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState, required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner, progress: ComposerProgressSink | None,
) -> SelectedContinuationState:
    clock = _end_phase_clock(selected)
    async def prepare() -> tuple[ContinuationFrameStack, tuple[PrivateChunk, ...], AdvisorFrame, AdvisorCheckpointPassRecord]:
        return await _prepare_continuation_publication_value(service=service, operation_authority=operation_authority,
            selected=selected, required_work=required_work, provider_owner=provider_owner, progress=progress)
    stack, values, child, record = await clock.run("physical", prepare)
    async def retain() -> SelectedContinuationState:
        return await _retain_continuation_publication_value(service=service, operation_authority=operation_authority,
            selected=selected, required_work=required_work, provider_owner=provider_owner, progress=progress,
            stack=stack, values=values, child=child, record=record)
    return await clock.run("retention", retain)
```

- [ ] Extract E3's `push_continuation_end_review` prefix, from its first local import through `stack = stack.model_copy(update={"frames": (*stack.frames, AdvisorFrame(...))})`, including the entire multiline expression, into `_prepare_continuation_end_review_value`. Preserve all seven original keywords, including `semantic:bool`; append the exact return below. The return type is `tuple[SelectedContinuationState,ContinuationFrameStack,tuple[PrivateChunk,...],ContinuationEvent]`. This prefix can commit the actual outer Review preparation and reload its complete head; its returned `selected` is that actual result, never the older caller head.

```python-statement
return selected, stack, values, ContinuationEvent("review_retained",
    canonical_json({"kind": source.kind, "review": source_chunk.reference.digest}))
```

Replace the public function with this complete wrapper and concrete retention operation:

```python
async def push_continuation_end_review(
    *, service: ComposerServiceImpl, operation_authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState, required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner, progress: ComposerProgressSink | None, semantic: bool,
) -> SelectedContinuationState:
    clock = _end_phase_clock(selected)
    async def prepare() -> tuple[SelectedContinuationState, ContinuationFrameStack, tuple[PrivateChunk, ...], ContinuationEvent]:
        return await _prepare_continuation_end_review_value(service=service, operation_authority=operation_authority,
            selected=selected, required_work=required_work, provider_owner=provider_owner, progress=progress, semantic=semantic)
    prepared_selected, stack, values, event = await clock.run("physical", prepare)
    async def retain() -> SelectedContinuationState:
        return await commit_continuation_phase(authority=operation_authority, running=_end_running(required_work),
            checkpoint_id=prepared_selected.checkpoint_id, previous_cursor=prepared_selected.stack.cursor,
            stack=stack, chunks=values, required_work=required_work, event=event)
    return await clock.run("retention", retain)
```

- [ ] Rename E4's complete `apply_continuation_semantic_review` to `_apply_continuation_semantic_value`, preserving its original complete body/keywords/return unchanged. The public wrapper below bounds the whole owned SQL acceptance within the retention allowance. Its runtime/current-binding reconstruction is included in this allowance as well, a stricter bound than spending a second preparation or execution allowance. It does not renew that allowance during retries or after a late ledger join.

```python
async def apply_continuation_semantic_review(
    *, service: ComposerServiceImpl, operation_authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState, required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner, progress: ComposerProgressSink | None,
) -> SelectedContinuationState:
    clock = _end_phase_clock(selected)
    async def retain() -> SelectedContinuationState:
        return await _apply_continuation_semantic_value(service=service, operation_authority=operation_authority,
            selected=selected, required_work=required_work, provider_owner=provider_owner, progress=progress)
    return await clock.run("retention", retain)
```

- [ ] Rename E5's full `apply_continuation_control_receipt` to `_apply_continuation_control_value`, unchanged. Add this complete bounded owed-application wrapper; it does not re-run the scheduling request or create another successor.

```python
async def apply_continuation_control_receipt(
    *, service: ComposerServiceImpl, operation_authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState, required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner, progress: ComposerProgressSink | None,
) -> SelectedContinuationState:
    clock = _end_phase_clock(selected)
    async def retain() -> SelectedContinuationState:
        return await _apply_continuation_control_value(service=service, operation_authority=operation_authority,
            selected=selected, required_work=required_work, provider_owner=provider_owner, progress=progress)
    return await clock.run("retention", retain)
```

- [ ] Rename E5's complete `finalize_continuation_end` to `_finalize_continuation_end_value`, unchanged, including its current terminal authorization read. Add this complete bounded deterministic projection wrapper. The returned proposal is not a committed terminal. The C4 terminal SQL and control-yield SQL each require their own actual session-SQL clock in their own runner task; a fit decision alone does not implement those other owners.

```python
async def finalize_continuation_end(
    *, service: ComposerServiceImpl, operation_authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState, required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner, progress: ComposerProgressSink | None,
) -> ContinuationAdvance:
    clock = _end_phase_clock(selected)
    async def execute() -> ContinuationAdvance:
        return await _finalize_continuation_end_value(service=service, operation_authority=operation_authority,
            selected=selected, required_work=required_work, provider_owner=provider_owner, progress=progress)
    return await clock.run("physical", execute)
```

These are local product-clock closures over actual owners. None is serialized, accepted from the model or used as an alternate business mutation authority. A timeout after actual SQL commit leaves the complete committed head selected; a timeout while a real worker future is still running retains its actual coordinator/lease/process custody until the existing finish-once/recovery owner resolves it. Never release a process lock because the clock's waiter ended.

- [ ] Replace only the entire `if boundary.cancel_requested:` branch in E5's outward dispatcher with `return await stop_continuation_end(operation_authority=operation_authority, selected=selected, required_work=required_work)`. Add this complete current Stop owner; its actual SQL/projection and original cancellation objects stay inside the retention stage. The durable Stop marker still has failure-reducer priority if the clock also expires.

```python
async def stop_continuation_end(
    *, operation_authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState, required_work: RequiredWorkBinding,
) -> Never:
    from elspeth.web.sessions.composer_continuation_stop import ComposerContinuationStopReady
    clock = _end_phase_clock(selected)
    async def retain() -> Never:
        sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL,
            RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
        try:
            outcome = await run_required_sql_finish_once(sql, operation_authority.prepare_continuation_stop,
                _end_running(required_work), expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.stack.cursor)
        except BaseException:
            projection.complete_without_submission()
            raise
        stored, cancellations = _end_sql_result(required_work=required_work, sql=sql, projection=projection,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL, outcome=outcome)
        await _end_selected(authority=operation_authority, required_work=required_work, stored=stored)
        cancelled = ComposerContinuationStopReady(claim=_end_running(required_work).claim,
            checkpoint_id=stored.checkpoint_id, cursor=stored.cursor)
        if cancellations:
            raise BaseExceptionGroup("Durable Stop and actual deferred cancellations", (cancelled, *cancellations))
        raise cancelled
    return await clock.run("retention", retain)
```

Import `Never` from `typing`. This Stop owner is reached only after the frames dispatcher drains actual completed issued results. B5 closes only the no-effect prepared-call/manual-input prefix. B9's separate exact selected-batch transaction refuses an actually prepared read/session-SQL owner only after proving no committed receipt, then F32 drains B6's owed P4 before entering this function. Already committed results take owed application. Keep the `ToolBatchFrame` refusal here: neither this function nor a timer discards an issued prefix or physical unknown.

### E8: Route only a SQL-prepared Stop to the terminal owner

**Create:** `src/elspeth/web/sessions/composer_continuation_stop.py`. **Modify:** the existing `_settle_failure`, `SessionServiceImpl.fail_composer_async_operation`/protocol, `settle_composer_operation_on_connection`, `settle_continuation_on_connection`, and F31's actual post-prepare boundary. The pure signal class and selector below land with the early pure contracts before the C3 terminal owner; the SQL/worker adapters land after their actual C3/C5 dependencies. This packet is prerequisite to O9/B9 full-driver Stop tests. An ordinary early `CancelledError` or `ComposerOperationCancelledDuringTurn` still takes actual recovery; neither means an issued P4 has drained.

- [ ] Add this complete closed signal and actual-leaf selector. The signal is a locator, never mutation authority. The final SQL transaction independently checks its exact current head, claim, durable Stop marker and absence of owed work.

```python
from __future__ import annotations

from uuid import UUID
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.composer_operations import ComposerOperationClaim


class ComposerContinuationStopReady(RuntimeError):
    def __init__(self, *, claim: ComposerOperationClaim, checkpoint_id: str, cursor: int) -> None:
        if (type(claim) is not ComposerOperationClaim or claim.segment_sequence is None
            or claim.ownership_generation is None or type(checkpoint_id) is not str
            or str(UUID(checkpoint_id)) != checkpoint_id or type(cursor) is not int or cursor < 0):
            raise AuditIntegrityError("Stop-ready signal requires its actual selected current continuation locator")
        self.claim = claim
        self.checkpoint_id = checkpoint_id
        self.cursor = cursor
        super().__init__("The durable Stop is prepared after its actual issued results were joined.")


def continuation_stop_ready_from_failure(error: BaseException) -> ComposerContinuationStopReady | None:
    leaves: list[ComposerContinuationStopReady] = []
    pending = [error]
    while pending:
        current = pending.pop()
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        elif type(current) is ComposerContinuationStopReady:
            leaves.append(current)
    if not leaves:
        return None
    first = leaves[0]
    if any((item.claim, item.checkpoint_id, item.cursor) != (first.claim, first.checkpoint_id, first.cursor) for item in leaves):
        raise AuditIntegrityError("Stop terminal contains different actual prepared locators")
    return first
```

- [ ] In the existing `_settle_failure`, after its actual retained roots are collected and before creating its failure projection, bind the selector below. Forward it unchanged in the actual `fail_composer_async_operation` call. This changes no failure rank or original object; W4's joined-cohort terminal routing includes exactly `type(leaf) is ComposerContinuationStopReady` alongside its existing protocol-error leaf branch.

```python-statement
continuation_stop_ready = continuation_stop_ready_from_failure(
    roots[0] if len(roots) == 1 else BaseExceptionGroup("Composer retained Stop roots", roots))
```

After the existing final `failure=self._failure_for(...)` selection, bind the exact forwarding value below. A stronger integrity/cohort failure retains its original rank and does not carry a Stop-only locator into another outcome. Import the selector normally from the new module. Add optional `continuation_stop_ready:ComposerContinuationStopReady|None=None` to `SessionServiceImpl.fail_composer_async_operation` and its protocol, importing the nominal type normally in the service and only under `TYPE_CHECKING` in the protocol. Its existing exact `_sync` call to `settle_composer_operation_on_connection` forwards that keyword unchanged. Do not add it to ordinary complete-success calls or other service methods.

```python-statement
selected_stop_ready = continuation_stop_ready if failure.failure_code == "request_cancelled" else None
```

The actual call member is `continuation_stop_ready=selected_stop_ready`. The signal is still present in the captured original failure facts even when that forwarding value is `None`.

Add this complete actual failure-selection control to the O9 integration module. The existing current failure reducer and real SQL failure publisher run unchanged. The spy forwards the selected outcome and proves a stronger actual integrity failure does not carry a Stop-only locator. Neither original object is replaced.

```python
@pytest.mark.asyncio
async def test_actual_stronger_failure_keeps_stop_signal_in_facts_without_forwarding_its_locator(
    operation_store, tmp_path, monkeypatch,
):
    from elspeth.web.sessions.composer_app_services import composer_app_services
    from elspeth.web.sessions.composer_continuation_stop import ComposerContinuationStopReady
    from tests.helpers.continuation_driver import driver_worker, failure_objects
    from tests.unit.web.sessions.test_continuation_cold_restore import start_continuation
    case = await seed_driver_case(operation_store, tmp_path / "stronger-stop-data")
    env = fresh_driver_environment(clone_admitted_database(case, tmp_path / "stronger-stop.db"), case)
    repository = env.sessions.session_operation_authority
    running = start_continuation(repository, env.authority)
    worker = driver_worker(env)
    actual_fail = env.sessions.fail_composer_async_operation
    forwarded = []
    try:
        stopped = env.authority.request_v2_cancel(session_id=case.session_id, operation_id=case.operation_id,
            actor_user_id="alice", auth_provider_type="local")
        assert stopped is not None and stopped[0].cancel_requested
        stored = env.authority.load_continuation_snapshot(running)
        signal = ComposerContinuationStopReady(claim=running.claim, checkpoint_id=stored.checkpoint_id, cursor=stored.cursor)
        stronger = AuditIntegrityError("controlled stronger actual cohort integrity failure")
        original = ExceptionGroup("actual Stop and stronger failure", [signal, stronger])
        async def fail(actual_running, **kwargs):
            assert actual_running is running and kwargs["continuation_stop_ready"] is None
            assert kwargs["failure"].failure_code != "request_cancelled"
            forwarded.append(kwargs["failure"])
            return await actual_fail(actual_running, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(env.sessions, "fail_composer_async_operation", fail)
            result = await worker._settle_failure(composer_app_services(env.app), running, original)
        assert len(forwarded) == 1 and result.status == "failed"
        assert result.failure_code == forwarded[0].failure_code
        assert any(item is signal for item in failure_objects(original))
        assert any(item is stronger for item in failure_objects(original))
        worker._stopping = True
        worker.assert_shutdown_complete()
    finally:
        repository.release(running.session_operation_context)
        env.dispose()
```

- [ ] Add optional `continuation_stop_ready:ComposerContinuationStopReady|None=None` to `settle_composer_operation_on_connection` and `settle_continuation_on_connection`. The former's v2 branch forwards it unchanged; immediately after that branch, before its ordinary v1 body, require it to be `None` or raise `AuditIntegrityError("legacy settlement cannot use a continuation Stop locator")`. Add this full validator to `sessions/continuation.py` and call it in the latter after `stored=load_snapshot(...)` and before any terminal append/update whenever `failed and outcome.failure_code=="request_cancelled"`.

```python
def require_continuation_stop_terminal_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, stored: StoredContinuationSnapshot,
    stop_ready: ComposerContinuationStopReady | None,
) -> None:
    from elspeth.web.composer.continuation import AdvisorFrame, ToolBatchFrame
    from elspeth.web.sessions.composer_continuation_stop import ComposerContinuationStopReady
    if not require_continuation_mutation(conn, running.session_operation_context,
        allow_cancel=True, allow_expired_segment=True):
        raise AuditIntegrityError("Stop terminal has no actual continuation history")
    sid, op = str(running.claim.session_id), running.claim.operation_id
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == op)).one()
    if parent.cancel_requested_at is None:
        raise AuditIntegrityError("Stop terminal lacks its current durable actual Stop marker")
    root = decode_continuation_snapshot(stored.payload_json)
    if stop_ready is not None:
        if (type(stop_ready) is not ComposerContinuationStopReady or stop_ready.claim != running.claim
            or (stop_ready.checkpoint_id, stop_ready.cursor) != (stored.checkpoint_id, stored.cursor)):
            raise ContinuationCASConflict("Stop terminal lost its exact prepared locator/current claim")
    if (root.pending_owner_results or root.pending_settlement_ids
        or any(type(frame) is ToolBatchFrame for frame in root.frames)
        or any(type(frame) is AdvisorFrame and frame.response is not None for frame in root.frames)):
        raise AuditIntegrityError("Stop terminal still owes actual batch/result/review projection")
    rows = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == op)).all()
    frontier = {item.effect_id: item for item in root.effect_frontier}
    if set(frontier) != {row.effect_key for row in rows}:
        raise AuditIntegrityError("Stop terminal omitted an actual execution effect")
    for row in rows:
        selected = frontier[row.effect_key]
        if (row.disposition not in ("committed", "refused")
            or (selected.disposition, selected.receipt_id) != (row.disposition, row.receipt_key)
            or row.result_chunk_key is None):
            raise AuditIntegrityError("Stop terminal still owes actual prepared/physical/result work")
    unsettled = conn.execute(select(provider_attempts.c.attempt_id).join(effects,
        effects.c.provider_attempt_id == provider_attempts.c.attempt_id).where(
            effects.c.session_id == sid, effects.c.operation_id == op,
            provider_attempts.c.settled_at.is_(None))).first()
    if unsettled is not None:
        raise AuditIntegrityError("Stop terminal still owes actual financial settlement")
```

The exact call is:

```python-statement
if failed and outcome.failure_code == "request_cancelled":
    require_continuation_stop_terminal_on_connection(conn, running, stored=stored, stop_ready=continuation_stop_ready)
elif continuation_stop_ready is not None:
    raise AuditIntegrityError("Stop locator cannot authorize another terminal outcome")
```

An independently selected stronger integrity/provider failure can still be projected by its ordinary owner; a durable Stop failure can commit only after the same actual no-owed-work proof. No signal or failure string bypasses that proof. Terminal retries which already read an immutable terminal row retain the existing idempotent return behavior.

- [ ] In F31 `run_continuation_prescribed_read`, immediately before constructing its `ContinuationOwnerCommit` after selected preparation/load, add this complete current SQL boundary check. It runs for an existing prepared read as well as a newly prepared one. If Stop arrived after preparation, return the actual selected state to F32; END then refuses only this exact read with its explicit receipt chunk. No new handler starts after that observed Stop.

```python-statement
boundary = await read_continuation_boundary(authority=operation_authority,
    selected=selected, required_work=required_work)
if boundary.cancel_requested:
    return selected
```

Use F28's actual facade name `read_continuation_boundary` and existing registered read owner; it binds the exact selected checkpoint/cursor. The new return follows a real preparation checkpoint. Existing prepared roots under a durable Stop are routed directly to END by F32 before invoking F31.

- [ ] Extend O9's actual P4 writer observer with the exact early-terminal negative control below, before forwarding the real P4 call. It tries to construct a premature locator at the actual selected P4 phase. The final SQL refuses and rolls back every audit/sequence/terminal byte, then the real P4/Stop flow must complete exactly once. The normal test's single actual worker must end `failed/request_cancelled`, so repeat suspension is a failed assertion.

```python-statement
from elspeth.web.sessions.composer_continuation_stop import ComposerContinuationStopReady
from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
commit = kwargs["continuation"]
early = ComposerContinuationStopReady(claim=commit.running.claim,
    checkpoint_id=commit.expected_checkpoint_id, cursor=commit.expected_cursor)
before = capture_tables(env.sessions._engine)
with pytest.raises(AuditIntegrityError, match="owes actual"):
    await env.sessions.fail_composer_async_operation(commit.running,
        failure=request_cancelled_error(request_id=None), continuation_stop_ready=early)
assert capture_tables(env.sessions._engine) == before
```

- [ ] Add this complete pure signal control to `tests/unit/web/composer/test_continuation_end.py`, importing the existing actual `running` fixture helper from its owner when obtaining the `claim` in the fixture. The actual full-driver O9 test supplies a real claim; no fabricated signal alone can pass the SQL test above.

```python
def test_stop_ready_selector_preserves_actual_signal_and_rejects_different_locators(operation_store):
    from uuid import uuid4
    from elspeth.web.sessions.composer_continuation_stop import (
        ComposerContinuationStopReady, continuation_stop_ready_from_failure,
    )
    from tests.unit.web.sessions.test_continuation_cold_restore import admit_continuation, start_continuation
    _engine, repository, authority, _service, _sid = operation_store
    admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    try:
        stored = authority.load_continuation_snapshot(running)
        first = ComposerContinuationStopReady(claim=running.claim, checkpoint_id=stored.checkpoint_id, cursor=stored.cursor)
        assert continuation_stop_ready_from_failure(first) is first
        ordinary = asyncio.CancelledError("ordinary earlier cancellation")
        assert continuation_stop_ready_from_failure(ordinary) is None
        grouped = BaseExceptionGroup("actual signal and cancellation", [ordinary, first])
        assert continuation_stop_ready_from_failure(grouped) is first
        other = ComposerContinuationStopReady(claim=running.claim, checkpoint_id=str(uuid4()), cursor=stored.cursor)
        with pytest.raises(AuditIntegrityError, match="different actual"):
            continuation_stop_ready_from_failure(ExceptionGroup("different locators", [first, other]))
    finally:
        repository.release(running.session_operation_context)
```

Run the O11 exact physical/P4 command and B9's exact actual Stop driver module after this packet. For behavioral RED temporarily remove only the exact `ComposerContinuationStopReady` leaf condition from W4; O9 must fail its single-attempt terminal assertion while all imports/setup succeed. Restore it and require native GREEN. This distinguishes the prepared signal from a plain cancellation and verifies actual SQL settlement rather than a mocked terminal result.

- [ ] Add this complete prescribed-read Stop test to the same real F33 integration module used by O9. Its semantic answer is raw external SDK text; the actual parser, model decision ID binding, action frontier, current policy/manifest producer, prepared read identity and SQL owners stay real. The handler tripwire forwards the actual function if reached, so the absence assertion is controlled by the real prepared-action observation.

```python
@pytest.mark.asyncio
async def test_actual_prescribed_read_stop_refuses_only_selected_input_and_never_dispatches_handler(
    operation_store, tmp_path, monkeypatch,
):
    from dataclasses import replace
    from sqlalchemy import update
    import elspeth.web.composer.continuation_driver as driver
    from tests.helpers.continuation_driver import SDKReply
    from elspeth.web.sessions.continuation import read_chunk
    case = await seed_driver_case(operation_store, tmp_path / "prescribed-stop-data")
    env = fresh_driver_environment(clone_admitted_database(case, tmp_path / "prescribed-stop.db"), case)
    script = ordinary_driver_script(case)
    script.replies["semantic"] = (SDKReply(semantic="continue"),)
    actual_prepare, actual_stop = env.authority.prepare_tool_action, env.authority.prepare_continuation_stop
    actual_handler = driver.execute_tool
    prepared, closed, dispatched = [], [], []

    async def sdk(**kwargs):
        response = await script.complete(**kwargs)
        if sdk_role(kwargs) == "semantic":
            value = json.loads(response.choices[0].message.content)
            action = value["prescribed_action"]
            action.pop("instruction")
            action.update(kind="read_tool", tool_name="get_plugin_schema",
                arguments_json=canonical_json({"plugin_type": "source", "name": "csv"}))
            text = canonical_json(value)
            response.choices[0].message.content = text
            script.invocations[-1] = replace(script.invocations[-1], response_content=text)
        return response

    def prepare(running, **kwargs):
        result = actual_prepare(running, **kwargs)
        if not kwargs["effect_key"].startswith("action:read:"):
            return result
        assert not prepared and kwargs["owner"] == "read"
        prepared.append((running, kwargs["effect_key"], kwargs["input_sha256"]))
        receipt = env.authority.request_v2_cancel(session_id=case.session_id, operation_id=case.operation_id,
            actor_user_id="alice", auth_provider_type="local")
        assert receipt is not None and receipt[0].cancel_requested
        selected = dict(expected_checkpoint_id=result.checkpoint_id, expected_cursor=result.cursor)
        before = capture_tables(env.sessions._engine)
        with env.sessions._engine.begin() as conn:
            conn.execute(update(composer_continuation_effects_table).where(
                composer_continuation_effects_table.c.effect_key == kwargs["effect_key"]).values(input_sha256="f"*64))
        corrupted = capture_tables(env.sessions._engine)
        with pytest.raises(AuditIntegrityError, match="immutable input"):
            actual_stop(running, **selected)
        assert capture_tables(env.sessions._engine) == corrupted
        with env.sessions._engine.begin() as conn:
            conn.execute(update(composer_continuation_effects_table).where(
                composer_continuation_effects_table.c.effect_key == kwargs["effect_key"])
                .values(input_sha256=kwargs["input_sha256"]))
        assert capture_tables(env.sessions._engine) == before
        return result

    def stop(running, **kwargs):
        result = actual_stop(running, **kwargs)
        assert prepared
        key, digest = prepared[0][1:]
        with env.sessions._engine.connect() as conn:
            effect = conn.execute(select(composer_continuation_effects_table).where(
                composer_continuation_effects_table.c.effect_key == key)).one()
            assert effect.disposition == "refused" and effect.receipt_key == f"stop:{key}"
            assert effect.result_chunk_key is not None and effect.tool_receipt_json is None
            value = json.loads(read_chunk(conn, session_id=str(case.session_id), operation_id=case.operation_id,
                key=effect.result_chunk_key))
        assert value == {"schema_version": "composer.prescribed-read-stop.v1", "effect_key": key,
            "input_sha256": digest, "owner": "read", "reason": "durable_stop_before_result_retention"}
        closed.append(result)
        return result

    def handler(*args, **kwargs):
        dispatched.append((args, kwargs))
        return actual_handler(*args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(litellm, "acompletion", sdk)
            patch.setattr(env.authority, "prepare_tool_action", prepare)
            patch.setattr(env.authority, "prepare_continuation_stop", stop)
            patch.setattr(driver, "execute_tool", handler)
            _escaping, parent = await finish_actual_failed_driver(env)
        assert len(prepared) == len(closed) == 1 and not dispatched
        assert parent.status == "failed" and parent.failure_code == "request_cancelled"
        assert script.consumed["semantic"] == 1
    finally:
        env.dispose()
```

For its behavioral RED, temporarily omit only F31's post-prepare current boundary check. The actual handler tripwire becomes nonempty or actual owned read result replaces the expected refusal; require native exit 1 with the behavioral assertion, then restore the check and require native exit 0. Add this exact test to O11's existing full integration-module command; it uses no separate fixture or controller.

- [ ] Add this complete actual-worker timing test to the E6 module. It retains a real completed pass transaction inside its real pending SQL future beyond V. The clock must eventually refuse continuation in that attempt, while the actual cohort/process custody stays held until the producer returns. The next fresh owner applies the already selected complete result without another review SDK call.

```python
@pytest.mark.asyncio
async def test_end_retention_clock_does_not_release_custody_of_a_real_sql_future(operation_store, tmp_path, monkeypatch):
    import threading
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    from elspeth.web.sessions.routes._helpers import composer_session_lock_registry
    from tests.helpers import continuation_driver as driver_helpers
    from tests.helpers.continuation_policy import minimal_continuation_policy
    policy = minimal_continuation_policy()
    local = policy.actions.session_sql.model_copy(update={"retention": 0.5})
    policy = policy.model_copy(update={"actions": policy.actions.model_copy(update={"session_sql": local})})
    entered, release = threading.Event(), threading.Event()
    original_sync = SessionServiceImpl._persist_continuation_advisor_pass_sync
    original_worker = driver_helpers.driver_worker
    workers, retained = [], []
    def worker_for(env):
        worker = original_worker(env)
        workers.append(worker)
        return worker
    def held_sync(self, **kwargs):
        actual = original_sync(self, **kwargs)
        if kwargs["record"].phase == "end" and not retained:
            retained.append(actual)
            entered.set()
            assert release.wait(timeout=15), "test did not release its actual completed SQL producer"
        return actual
    env = None
    owner = None
    with monkeypatch.context() as patch:
        patch.setattr(driver_helpers, "minimal_continuation_policy", lambda: policy)
        case = await seed_driver_case(operation_store, tmp_path / "data")
        database = clone_admitted_database(case, tmp_path / "timed.db")
        env = fresh_driver_environment(database, case)
        sdk = ordinary_driver_script(case)
        patch.setattr("litellm.acompletion", sdk.complete)
        patch.setattr(driver_helpers, "driver_worker", worker_for)
        patch.setattr(SessionServiceImpl, "_persist_continuation_advisor_pass_sync", held_sync)
        try:
            owner = asyncio.create_task(run_driver_attempt(env, sdk, patch, expect_suspension=True))
            deadline = asyncio.get_running_loop().time() + 15
            while not entered.is_set():
                assert not owner.done(), "actual END owner stopped before the controlled SQL producer"
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.01)
            await asyncio.sleep(1.0)
            assert not owner.done() and len(workers) == 1 and len(retained) == 1
            worker = workers[0]
            key = (case.session_id, case.operation_id)
            assert not worker._required_coordinators[key].all_completed
            lock = await composer_session_lock_registry(env.app).get_lock(str(case.session_id))
            assert lock.locked() and key not in worker._continuation_outcomes
            with env.sessions._engine.connect() as conn:
                actual = load_snapshot(conn, session_id=str(case.session_id), operation_id=case.operation_id)
                assert actual == retained[0]
                root = decode_continuation_snapshot(actual.payload_json)
                assert root.frames[-1].frame_kind == "end" and root.frames[-1].phase == "publication_ready"
            release.set()
            await owner
            assert not lock.locked()
            env.dispose()
            env = fresh_driver_environment(database, case)
            await run_driver_attempt(env, sdk, patch)
            sdk.assert_consumed()
            assert len(actual_end_pass_rows(env.sessions._engine, case.session_id)) == 1
            assert sum(item.role == "end" for item in sdk.invocations) == 1
            assert "Final exact café description" in read_driver_outcome(env, sdk).graphs_json
        finally:
            release.set()
            if owner is not None and not owner.done():
                await asyncio.gather(owner, return_exceptions=True)
            if env is not None:
                env.dispose()
```

Behavioral RED: bypass only the E7 received-publication retention `clock.run("retention", retain)` with `await retain()`. The actual worker then completes normally after the held producer returns and `expect_suspension=True` must fail. Restore the bound and require this real-worker test plus E6's module command to finish with native exit 0. The test never reports a canceled waiter as proof that its SQL future finished.
