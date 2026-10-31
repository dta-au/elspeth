# Composer Continuation Driver Equivalence Test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove that the actual Composer driver resumes the original authorized request through fresh owners with the same provider wire, graph effects, review debt and accounting as uninterrupted execution.

**Architecture:** Seed a real SQLite store through the existing operation-store fixture and actual blob/state/admission owners. At each selected baseline boundary, copy the actually committed database, let the uninterrupted walk finish, then resume the copy through fresh owners. Compare the first subsequent SDK request byte-for-byte and separately compare the complete SQL outcomes through validated nominal relationships. Replace only `litellm.acompletion` with an explicit deterministic SDK transcript; normal required-work, lease, worker, quota, recovery and publication owners still execute.

**Tech Stack:** pytest/pytest-asyncio, real LiteLLM response objects, SQLAlchemy, SQLite backup, existing Composer worker/process-watchdog fixtures and actual session/continuation owners.

This is an implementation-plan packet; its future driver tests have not run.
Immutable extraction anchors use
`b003e0cd9c6c6a7b0968635a9c03f71a83bc2e73`, tree
`6a34e912debc469e9ce7e2048d433c08d5e606a9`. The main plan records the current
combined source, actual carried fixes and qualification separately. Local
`.9` qualification is complete and the isolated integration exists. Run
this packet only after its literal new source owners are implemented there.
It adds no capacity reservation, controller or approval ceremony.

The canonical production interfaces consumed here are the actual planned
`ComposerServiceImpl.advance_continuation`, C4's
`run_composer_continuation_segment`, C3's selected snapshot/chunk readers and
actual SQL writers. Their concrete bodies are being authored in the frames
and storage companions. A missing implementation or a collected test that
cannot reach a named cut is a dependency failure, not equivalence evidence.

## D0: Freeze coverage and owner dependencies before running the packet

**Read:** main C1/C4/C5/C6, storage C1b/C3/C4a/C5 and frames F1–F7 plus its
bootstrap/driver task; the trusted-producer and batch completion packets.
**Create:** `tests/helpers/continuation_driver.py` and
`tests/integration/web/composer/test_continuation_driver_equivalence.py`.
**Create separately:**
`tests/integration/web/composer/test_continuation_driver_process_death.py`.

- [ ] Implement frames' complete phase driver and core's batch/P4 adapter
  before this packet. Do not substitute a retained `ComposerResult` or patch
  `compose`, `advance_continuation`, `execute_tool`, preflight, schema
  admission, proposal/interpretation writers, quota admission or terminal
  construction to return fixture results. The observation wrappers below
  call the real implementation and change only when the test cancels the
  actual task.
- [ ] Keep durable Advisor custody separate from C6 semantic context. Manual
  and EARLY calls retain their existing prompt and response schema. An exact
  `ProviderCallCustody.continuation_binding` activates durable stage timing
  and complete text-response retention for **all** durable Advisor calls;
  optional `ContinuationAdvisorContext` remains restricted to its owned END
  semantic question. Otherwise the manual/EARLY cut cases cannot pass.
- [ ] Bind every cut to a successfully selected actual checkpoint and its
  complete chunks. Expected coverage is ingress inserted/ready; primary
  response retained; a completed prefix of one issued multi-call batch;
  manual/EARLY/END Advisor response retained; P4 result retained before pure
  application; real repair entry; and the terminal publication transaction.
  A selector that never fires fails the case. It must not silently downgrade
  the case to a later ordinary boundary.
- [ ] Distinguish three physical states: positive no-SDK admission refusal,
  actual SDK entry with unknown result, and complete retained response with
  exact known usage. Only the third can resume without another SDK call;
  unknown stays reconciling with unknown accounting. The existing storage
  physical-recovery tests remain required alongside this full driver packet.
- [ ] Implement producers T6–T10 and attachment-history A2a before native
  cold-equivalence tests. Those packets define the exact selected-source
  reconstruction, current nominal builders and fresh full-original use
  inspection. Plain JSON blocks and saved guard facts grant no current
  authority. A native case must retain its complete block and ordering.

## D1: Build independent real stores and fresh production owners

**Create:** `tests/helpers/continuation_driver.py` with the following imports
and fixture foundation. `operation_store` remains the existing real fixture
from `tests/unit/web/coordination/test_composer_operation_authority.py`.
The fixed graph below is fixture input saved through the actual state writer;
the production authoring path still requires its provider for every authored
change. The copy is made only after all seed writes have completed. Each
walk opens a separate database with the same original IDs and source bytes.

- [ ] Add this complete foundation. It contains no fake catalog, graph
  validator, session service, secret inventory or quota owner.

```python
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import litellm
import pytest
import structlog
from fastapi import FastAPI
from sqlalchemy import select, update

from elspeth.contracts.chargeable_admission import ChargeableAdmissionPolicy
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.composer.advisor_output import ContinuationEvidenceRef
from elspeth.web.composer.continuation import (
    AdvisorFrame, ComposeFrame, EndFrame, InitializeFrame, ToolBatchFrame,
)
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot
from elspeth.web.composer.progress import ComposerProgressRegistry
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.rate_limit_authority import ComposerQuotaAdmission, RepositoryRateLimitAuthority
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from elspeth.web.secrets.server_store import ServerSecretStore
from elspeth.web.secrets.service import ScopedSecretResolver, WebSecretService
from elspeth.web.secrets.user_store import UserSecretStore
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions.composer_operations import composer_operation_v2_request_hash
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    chat_messages_table, composition_states_table, composition_proposals_table,
    interpretation_events_table, quota_provider_attempts_table, token_usage_ledger_table,
    composer_async_operations_table, quota_policies_table,
    composer_segment_attempts_table, composer_continuation_effects_table,
)
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.schemas import SendMessageRequest
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import grant_test_pipeline_user
from tests.helpers.continuation_policy import minimal_continuation_policy
from tests.helpers.fenced_session import seed_token_policies
from tests.unit.web.composer._helpers import _make_settings


REQUEST = (
    "Preserve café and exact context. Inspect the CSV schema and update the pipeline description. "
    "Keep the ordinary label 00000000-0000-4000-8000-000000009999 verbatim."
)


@dataclass(frozen=True, slots=True)
class AdmittedDriverCase:
    source_database: Path
    data_dir: Path
    session_id: UUID
    operation_id: str
    base_state_id: UUID
    original_user_message_id: str
    pipeline_json: str


async def seed_driver_case(
    operation_store, data_dir: Path, *, empty: bool = False,
    trust_mode: Literal["auto_commit", "explicit_approve"] = "auto_commit",
) -> AdmittedDriverCase:
    engine, repository, _old_authority, sessions, session_id = operation_store
    with engine.begin() as conn:
        grant_test_pipeline_user(conn, identity_id="alice")
        identity_policy, container_policy = seed_token_policies(conn, identity_id="alice")
        conn.execute(update(quota_policies_table).where(
            quota_policies_table.c.policy_id.in_((identity_policy, container_policy)),
        ).values(tokens_per_day=10_000_000))
    context = repository.acquire(
        session_id=session_id, operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="test-owner", lease_seconds=30,
    )
    blobs = BlobServiceImpl(engine, data_dir, session_operation_authority=repository)
    try:
        blob = await blobs.create_blob(
            session_id, "input.csv", b"value\nalpha\nbeta\n", "text/csv",
            session_operation_context=context,
        )
        pipeline = {
            "source": {
                "plugin": "csv", "on_success": "rows", "on_validation_failure": "discard",
                "options": {"path": blob.storage_path, "blob_ref": str(blob.id), "schema": {"mode": "observed"}},
            },
            "nodes": [], "edges": [],
            "outputs": [{
                "sink_name": "rows", "plugin": "json", "on_write_failure": "discard",
                "options": {
                    "path": str(data_dir / "outputs" / str(session_id) / "result.jsonl"),
                    "schema": {"mode": "observed"}, "format": "jsonl", "mode": "write",
                    "collision_policy": "auto_increment",
                },
            }],
        }
        base = await sessions.save_composition_state(
            session_id,
            CompositionStateData(
                source=None if empty else {
                    "plugin": "csv", "on_success": "rows", "on_validation_failure": "discard",
                    "options": {"path": blob.storage_path, "blob_ref": str(blob.id), "schema": {"mode": "observed"}},
                },
                nodes=(), edges=(),
                outputs=() if empty else ({
                    "name": "rows", "plugin": "json", "on_write_failure": "discard",
                    "options": {
                        "path": str(data_dir / "outputs" / str(session_id) / "result.jsonl"),
                        "schema": {"mode": "observed"}, "format": "jsonl", "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                },),
                metadata_={"name": "Continuation fixture", "description": "Original description"},
            ),
            provenance="session_seed", session_operation_context=context,
        )
    finally:
        repository.release(context)
    await sessions.update_composer_preferences(
        session_id, trust_mode=trust_mode, density_default="high", actor="user:alice",
    )
    policy = minimal_continuation_policy()
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id="test-owner", claim_lease_seconds=30,
        continuation_policy=policy,
    )
    request = SendMessageRequest(operation_id=str(uuid4()), content=REQUEST, state_id=base.id)
    record, fresh = authority.admit(
        quota=ComposerQuotaAdmission(
            RepositoryRateLimitAuthority(engine, signing_key=b"operation-test-quota-key-32-bytes"), 10000,
        ),
        session_id=session_id, operation_id=request.operation_id, kind="compose_message",
        request_hash=composer_operation_v2_request_hash(session_id=session_id, kind="compose_message", request=request),
        actor_user_id="alice", request_id="driver-equivalence", base_state_id=base.id,
        request_json=request.model_dump_json(), deadline_seconds=85.0, max_nonterminal=64,
        auth_provider_type="local", lifecycle_version=2, continuation_enabled=True,
    )
    assert fresh and record.lifecycle_version == 2
    assert record.reserved_user_message_id is not None
    with engine.connect() as conn:
        assert conn.execute(select(chat_messages_table).where(
            chat_messages_table.c.session_id == str(session_id),
            chat_messages_table.c.role == "user",
        )).all() == []
    assert engine.url.database is not None
    return AdmittedDriverCase(
        Path(engine.url.database), data_dir, session_id, record.operation_id,
        base.id, record.reserved_user_message_id, canonical_json(pipeline),
    )


def clone_admitted_database(case: AdmittedDriverCase, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    assert not destination.exists()
    with sqlite3.connect(str(case.source_database)) as source:
        with sqlite3.connect(str(destination)) as target:
            source.backup(target)
    return f"sqlite:///{destination}"


@dataclass(frozen=True, slots=True)
class CurrentSnapshotFactory:
    snapshot: PluginAvailabilitySnapshot

    def for_user_id(self, _user_id: str) -> PluginAvailabilitySnapshot:
        return self.snapshot


@dataclass(slots=True)
class DriverEnvironment:
    app: FastAPI
    sessions: SessionServiceImpl
    composer: ComposerServiceImpl
    authority: ComposerAsyncOperationAuthority
    database_url: str
    case: AdmittedDriverCase
    evidence_observations: list[tuple[ContinuationEvidenceRef, ...]] = field(default_factory=list)

    def dispose(self) -> None:
        self.sessions._engine.dispose()


def fresh_driver_environment(
    database_url: str, case: AdmittedDriverCase, *, lease_seconds: int = 30,
) -> DriverEnvironment:
    engine = create_session_engine(database_url)
    repository = SQLiteLocalSessionOperationAuthority(engine)
    settings = _make_settings(
        data_dir=case.data_dir,
        composer_model="openai/gpt-4o", composer_advisor_model="openai/gpt-4o-mini",
        composer_endpoint_base_url="https://sdk.invalid/v1",
        composer_endpoint_api_key="continuation-test-only-primary-key",  # secret-scan: allow-this-line
        composer_advisor_endpoint_base_url="https://sdk.invalid/v1",
        composer_advisor_endpoint_api_key="continuation-test-only-advisor-key",  # secret-scan: allow-this-line
        composer_max_composition_turns=12, composer_max_discovery_turns=12,
    )
    catalog = create_catalog_service()
    runtime_config = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime_config)
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime_config)
    sessions = SessionServiceImpl(
        engine, data_dir=case.data_dir, telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.continuation-driver"),
        session_operation_authority=repository, owner_instance_id="test-owner",
        session_operation_lease_seconds=lease_seconds,
        chargeable_admission_policy=ChargeableAdmissionPolicy(
            identity_token_quota_configured=True,
            secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash,
        ),
    )
    secrets = ScopedSecretResolver(
        WebSecretService(
            user_store=UserSecretStore(engine, master_key="continuation-fixture-only-master-key"),
            server_store=ServerSecretStore(allowlist=()),
        ),
        auth_provider_type="local",
    )
    composer = ComposerServiceImpl.for_trained_operator(
        catalog=catalog, settings=settings, sessions_service=sessions,
        session_engine=engine, secret_service=secrets,
        blob_service=BlobServiceImpl(engine, case.data_dir, session_operation_authority=repository),
    )
    assert composer._availability.available, composer._availability.reason
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id="test-owner", claim_lease_seconds=30,
        continuation_policy=minimal_continuation_policy(),
    )
    app = FastAPI()
    app.state.session_service = sessions
    app.state.session_engine = engine
    app.state.composer_service = composer
    app.state.settings = settings
    app.state.catalog_service = catalog
    app.state.operator_profile_registry = profiles
    app.state.scoped_secret_resolver = secrets
    app.state.interpretation_surfacing = composer._interpretation_surfacing
    app.state.plugin_snapshot_factory = CurrentSnapshotFactory(PluginAvailabilitySnapshot.for_trained_operator(catalog))
    app.state.composer_progress_registry = ComposerProgressRegistry()
    app.state.composer_async_operation_authority = authority
    return DriverEnvironment(app, sessions, composer, authority, database_url, case)
```

All fixture paths above are pytest-owned project-local temporary paths from
the main execution setup's `TMPDIR`, not production paths. No actual endpoint
is contacted: both roles use explicit synthetic transport configuration and
the sole SDK entry is replaced in D2. A test accidentally bypassing that fake
must fail the repository's normal unmocked-SDK guard.

## D2: Substitute only the SDK and retain every complete request

**Append:** `tests/helpers/continuation_driver.py`.
**Test:** the integration module in D5. The provider script is test input,
not production graph synthesis. The script offers ordinary registered tools
and exact model answers. All decoding, proposal/interpretation creation,
runtime preflight, response retention and publication still run for real.

- [ ] Add these complete SDK script types. A missing/extra transition fails
  immediately. Metadata is returned in a real `litellm.ModelResponse` so
  actual field admission, usage extraction and native replay are exercised.
  The trace excludes only purpose-specific live transport configuration;
  complete messages/tools/options and native block bytes remain present.

```python
from elspeth.web.composer.continuation_actions import ContinuationAdvisorContext


SDKRole = Literal["primary", "manual", "early", "end", "semantic", "title"]


@dataclass(frozen=True, slots=True)
class ToolReply:
    call_id: str
    name: str
    arguments_json: str

    def wire(self) -> dict[str, Any]:
        return {
            "id": self.call_id, "type": "function",
            "function": {"name": self.name, "arguments": self.arguments_json},
        }


@dataclass(frozen=True, slots=True)
class SDKReply:
    content: str | None = None
    calls: tuple[ToolReply, ...] = ()
    native: tuple[dict[str, str], ...] = ()
    semantic: Literal["stop", "continue"] | None = None


@dataclass(frozen=True, slots=True)
class SDKInvocation:
    role: SDKRole
    role_ordinal: int
    wire_json: str
    response_content: str | None


def sdk_role(kwargs: dict[str, Any]) -> SDKRole:
    if "response_format" in kwargs:
        name = kwargs["response_format"]["json_schema"]["name"]
        names: dict[str, SDKRole] = {
            "advisor_checkpoint_response": "early",
            "advisor_end_response": "end",
            "composer_continuation_decision": "semantic",
        }
        assert name in names, f"Unexpected response schema: {name}"
        return names[name]
    if "tools" in kwargs:
        return "primary"
    if kwargs.get("max_tokens") == 40:
        return "title"
    return "manual"


def complete_semantic_answer(reply: SDKReply, messages: list[dict[str, Any]]) -> str:
    contexts: list[ContinuationAdvisorContext] = []
    for message in messages:
        if message["role"] != "user" or type(message["content"]) is not str:
            continue
        try:
            value = json.loads(message["content"])
        except json.JSONDecodeError:
            continue
        if type(value) is dict and "question_id" in value:
            contexts.append(ContinuationAdvisorContext.model_validate_json(message["content"]))
    assert len(contexts) == 1, "Semantic SDK input must contain its one complete current context"
    context = contexts[0]
    obligations = [
        {
            "obligation_id": item.obligation_id, "parent_id": item.parent_id,
            "description": item.description, "original_message_id": item.original_message_id,
            "start": item.start, "end": item.end,
            "status": "satisfied" if reply.semantic == "stop" else item.status,
        }
        for item in context.frontier.obligations
    ]
    open_ids = [item["obligation_id"] for item in obligations if item["status"] == "open"]
    action = None
    if reply.semantic == "continue":
        assert open_ids
        action = {
            "kind": "primary_turn", "obligation_ids": open_ids,
            "evidence_ids": [item.receipt_id for item in context.frontier.evidence],
            "input_hash": context.input_hash, "graph_hash": context.frontier.graph_hash,
            "policy_hash": context.policy_hash, "tool_manifest_hash": context.tool_manifest_hash,
            "instruction": "Apply the remaining pipeline description correction, then review the actual result.",
        }
    assert reply.semantic is not None
    return canonical_json({
        "schema_version": "composer.continuation-decision.v1",
        "decision": "STOP_CONFIRMED" if reply.semantic == "stop" else "CONTINUE",
        "scope": "whole_request" if reply.semantic == "stop" else "subset",
        "obligation_ids": [item["obligation_id"] for item in obligations],
        "obligations": obligations,
        "evidence": [item.model_dump(mode="json") for item in context.frontier.evidence],
        "reason": "The complete current request is covered by the actual evidence." if reply.semantic == "stop"
                  else "The remaining description correction still requires the primary author.",
        "remaining_actionable_ids": open_ids,
        "prescribed_action": action,
    })


@dataclass(slots=True)
class DeterministicDriverSDK:
    replies: dict[SDKRole, tuple[SDKReply, ...]]
    consumed: dict[SDKRole, int] = field(default_factory=dict)
    invocations: list[SDKInvocation] = field(default_factory=list)

    async def complete(self, **kwargs: Any) -> litellm.ModelResponse:
        role = sdk_role(kwargs)
        ordinal = self.consumed.get(role, 0)
        assert role in self.replies and ordinal < len(self.replies[role]), f"Unexpected extra {role} provider call"
        reply = self.replies[role][ordinal]
        self.consumed[role] = ordinal + 1
        content = complete_semantic_answer(reply, kwargs["messages"]) if role == "semantic" else reply.content
        safe = {
            key: deepcopy(value) for key, value in kwargs.items()
            if key not in ("api_key", "extra_headers", "timeout")
        }
        self.invocations.append(SDKInvocation(role, ordinal, canonical_json(safe), content))
        if reply.calls:
            offered = {item["function"]["name"] for item in kwargs["tools"]}
            assert {item.name for item in reply.calls} <= offered
        message: dict[str, Any] = {
            "role": "assistant", "content": content,
            "tool_calls": [item.wire() for item in reply.calls] if reply.calls else None,
        }
        if reply.native:
            message["thinking_blocks"] = deepcopy(list(reply.native))
        return litellm.ModelResponse(
            id=f"sdk-{role}-{ordinal}", model=kwargs["model"],
            choices=[{"message": message, "finish_reason": "tool_calls" if reply.calls else "stop"}],
            usage={
                "prompt_tokens": 17 + ordinal, "completion_tokens": 11 + ordinal,
                "total_tokens": 28 + 2 * ordinal,
                "prompt_tokens_details": {"cached_tokens": 3},
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
        )

    def assert_consumed(self) -> None:
        assert self.consumed == {role: len(values) for role, values in self.replies.items() if values}


def tool_reply(call_id: str, name: str, arguments: dict[str, Any]) -> ToolReply:
    return ToolReply(call_id, name, canonical_json(arguments))


def clean_checkpoint(*, publication: bool) -> SDKReply:
    value: dict[str, Any] = {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None}
    if publication:
        value["publication_verdict"] = "ADMIT"
    return SDKReply(content=canonical_json(value))


def ordinary_driver_script(case: AdmittedDriverCase, *, early: bool = False, native: bool = False) -> DeterministicDriverSDK:
    blocks = (
        {"type": "thinking", "thinking": "native prefix " + "λ" * 9000 + " native tail", "signature": "opaque-signature-A"},
        {"type": "redacted_thinking", "data": "opaque-redacted-B"},
        {"type": "thinking", "thinking": "native second", "signature": "opaque-signature-C"},
    ) if native else ()
    discovery = SDKReply(
        content="I will inspect both current schemas.", native=blocks,
        calls=(
            tool_reply("schema:source", "get_plugin_schema", {"plugin_type": "source", "name": "csv"}),
            tool_reply("schema:sink", "get_plugin_schema", {"plugin_type": "sink", "name": "json"}),
        ),
    )
    first_mutation = tool_reply("author:initial", "set_pipeline", {"pipeline": json.loads(case.pipeline_json)}) if early else tool_reply(
        "author:initial", "set_metadata", {"patch": {"description": "Executed prefix description"}},
    )
    batch = SDKReply(
        content="I will retain the completed prefix before continuing the issued batch.",
        calls=(
            first_mutation,
            tool_reply("advisor:manual", "request_advisor_hint", {
                "trigger": "proactive_security_safety", "problem_summary": "Review the current CSV pipeline.",
                "recent_errors": [], "attempted_actions": [],
            }),
            tool_reply("author:suffix", "set_metadata", {"patch": {"description": "Final exact café description"}}),
        ),
    )
    replies: dict[SDKRole, tuple[SDKReply, ...]] = {
        "primary": (discovery, batch, SDKReply(content="The pipeline description is updated; the CSV data remains unchanged.")),
        "manual": (SDKReply(content="Keep the current source and output identities; the requested description is the only change."),),
        "semantic": (SDKReply(semantic="stop"),),
        "end": (clean_checkpoint(publication=True),),
    }
    if early:
        replies["early"] = (clean_checkpoint(publication=False),)
    return DeterministicDriverSDK(replies)


def repair_driver_script(case: AdmittedDriverCase) -> DeterministicDriverSDK:
    script = ordinary_driver_script(case)
    script.replies["primary"] = (*script.replies["primary"],
        SDKReply(content="I will apply the concrete review correction.", calls=(
            tool_reply("author:repair", "set_metadata", {"patch": {"description": "Repaired exact café description"}}),
        )),
        SDKReply(content="The reviewed correction is applied to the saved pipeline description."),
    )
    script.replies["end"] = (
        SDKReply(content=canonical_json({
            "verdict": "FLAGGED", "category": "other",
            "steps": ["Correct the saved description before publishing the candidate."],
            "findings": "The saved description needs the stated correction.",
            "note": None, "publication_verdict": "WITHHOLD",
        })),
        clean_checkpoint(publication=True),
    )
    # The actual END publication gate runs first. C6 is asked only after the
    # second, current CLEAN/ADMIT review has settled matching review debt.
    script.replies["semantic"] = (SDKReply(semantic="stop"),)
    return script
```

The fixed expected call counts are deliberate counterevidence. If the real
driver asks an extra semantic question, repeats a retained manual/EARLY call,
or emits a hidden primary retry, this script fails. Adjust an expected script
only after reading and accepting the resulting product transition; never
make it return unlimited generic CLEAN/STOP responses.

## D3: Interrupt only after an actual selected owner boundary

**Append:** `tests/helpers/continuation_driver.py`.
The wrapper below is an observation/fault seam, not an alternate driver. It
awaits the actual instance method, selects the SQL checkpoint and complete
chunks, and parks **after** that body. An unsupported phase or a body that
never commits the named boundary fails the cut test. For a writer whose
retained result is applied within one driver call, D3's SQL observer wrapper
below pauses after that actual writer instead; it never substitutes a receipt.

- [ ] Add these complete snapshot selectors. No selector invents a frame or
  changes a phase. Manual/EARLY/END selectors additionally bind the SDK call's
  actual phase, so a generic Advisor cut cannot certify all three.

```python
from elspeth.web.process_recovery import ProcessRecovery
from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
from elspeth.web.sessions.continuation import load_private_chunks, load_snapshot
from elspeth.web.sessions.routes._helpers import composer_session_lock_registry
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog


CutName = Literal[
    "ingress", "primary_response", "partial_batch", "manual_response",
    "early_response", "end_response", "p4_retained", "repair", "publication",
    "business_prepared",
]


@dataclass(frozen=True, slots=True)
class SelectedDriverState:
    checkpoint_id: str
    cursor: int
    payload_json: str
    chunks: tuple[tuple[str, str], ...]
    parent_status: str
    frame_kind: str
    frame_phase: str


def select_driver_state(env: DriverEnvironment) -> SelectedDriverState:
    with env.sessions._engine.connect() as conn, conn.begin():
        stored = load_snapshot(conn, session_id=str(env.case.session_id), operation_id=env.case.operation_id)
        stack = decode_continuation_snapshot(stored.payload_json)
        chunks = load_private_chunks(
            conn, session_id=str(env.case.session_id), operation_id=env.case.operation_id, stack=stack,
        )
        parent = conn.execute(select(composer_async_operations_table).where(
            composer_async_operations_table.c.session_id == str(env.case.session_id),
            composer_async_operations_table.c.operation_id == env.case.operation_id,
        )).one()
    assert stack.cursor == stored.cursor
    assert stack.logical.operation_id == env.case.operation_id
    assert stack.logical.original_user_message_id == env.case.original_user_message_id
    top = stack.frames[-1]
    phase = top.ingress_phase if type(top) is InitializeFrame else top.phase
    return SelectedDriverState(
        stored.checkpoint_id, stored.cursor, stored.payload_json,
        tuple((chunk.reference.digest, chunk.canonical) for chunk in chunks),
        parent.status, top.frame_kind, phase,
    )


def matches_cut(name: CutName, selected: SelectedDriverState, sdk: DeterministicDriverSDK) -> bool:
    stack = decode_continuation_snapshot(selected.payload_json)
    top = stack.frames[-1]
    if name == "ingress":
        return type(top) is InitializeFrame and top.ingress_phase in ("inserted", "ready")
    if name == "primary_response":
        return type(top) is ComposeFrame and top.phase == "response_retained"
    if name == "partial_batch":
        return (type(top) is ToolBatchFrame and top.phase == "tool_ready" and top.next_tool_index == 1
                and bool(sdk.invocations) and sdk.invocations[-1].role == "primary"
                and sdk.invocations[-1].role_ordinal == 1)
    if name in ("manual_response", "early_response", "end_response"):
        expected: SDKRole = {"manual_response": "manual", "early_response": "early", "end_response": "end"}[name]
        return (type(top) is AdvisorFrame and top.phase == "response_retained"
                and bool(sdk.invocations) and sdk.invocations[-1].role == expected)
    if name == "p4_retained":
        return (sdk.consumed.get("primary", 0) >= 2
                and any(item.kind == "persisted_turn" for item in stack.pending_owner_results))
    if name == "repair":
        return type(top) is EndFrame and top.phase == "repair_needed"
    if name == "business_prepared":
        from elspeth.web.composer.continuation_tool_batch import decode_batch_chunk
        if type(top) is not ToolBatchFrame:
            return False
        payloads = tuple(value for digest, value in selected.chunks if digest == top.accumulator.digest)
        if len(payloads) != 1:
            raise AssertionError("prepared SQL cut lost its actual selected accumulator")
        batch = decode_batch_chunk(PrivateChunk(top.accumulator, payloads[0]))
        call = batch.current_call
        return (call is not None and call.phase == "prepared"
            and call.tool_name in ("set_metadata", "request_interpretation_review")
            and call.effect_key is not None and call.owner_result_kind is None
            and call.owner_result_json is None and call.owner_tool_receipt_json is None
            and not stack.pending_owner_results
            and any(item.effect_id == call.effect_key and item.disposition == "prepared"
                    for item in stack.effect_frontier))
    return selected.parent_status == "completed"


@dataclass(slots=True)
class DriverCutObservation:
    name: CutName
    copy_to: Path | None = None
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    selected: SelectedDriverState | None = None
    sdk_prefix: tuple[SDKInvocation, ...] = ()
    sdk_consumed: dict[SDKRole, int] = field(default_factory=dict)

    async def observe(self, env: DriverEnvironment, sdk: DeterministicDriverSDK) -> None:
        if self.selected is not None:
            return
        actual = select_driver_state(env)
        if not matches_cut(self.name, actual, sdk):
            return
        self.selected = actual
        self.sdk_prefix = tuple(sdk.invocations)
        self.sdk_consumed = dict(sdk.consumed)
        if self.copy_to is not None:
            assert env.sessions._engine.url.database is not None
            self.copy_to.parent.mkdir(parents=True, exist_ok=True)
            assert not self.copy_to.exists()
            with sqlite3.connect(env.sessions._engine.url.database) as source:
                with sqlite3.connect(str(self.copy_to)) as target:
                    source.backup(target)
        self.reached.set()
        await self.release.wait()


def observe_real_driver(env: DriverEnvironment, sdk: DeterministicDriverSDK, cut: DriverCutObservation, monkeypatch) -> None:
    # Public advance returns only a safe boundary. Retained responses and
    # pending P4 are deliberately unsafe and must be observed at their real
    # SQL owner, before the driver applies them and advances again.
    loop = asyncio.get_running_loop()
    event_thread = threading.get_ident()

    def observe_after_required_sql() -> None:
        assert threading.get_ident() != event_thread, "Required SQL must use its actual owned executor"
        observed = asyncio.run_coroutine_threadsafe(cut.observe(env, sdk), loop)
        observed.result(timeout=35)

    actual_transition = env.authority.commit_transition

    def transition(*args, **kwargs):
        committed = actual_transition(*args, **kwargs)
        observe_after_required_sql()
        return committed

    monkeypatch.setattr(env.authority, "commit_transition", transition)
    actual_application = env.authority.commit_owner_application

    def application(*args, **kwargs):
        committed = actual_application(*args, **kwargs)
        observe_after_required_sql()
        return committed

    monkeypatch.setattr(env.authority, "commit_owner_application", application)
    if cut.name == "business_prepared":
        actual_prepare = env.authority.prepare_tool_action

        def prepare(*args, **kwargs):
            committed = actual_prepare(*args, **kwargs)
            if kwargs["owner"] == "session_sql":
                observe_after_required_sql()
            return committed

        monkeypatch.setattr(env.authority, "prepare_tool_action", prepare)
    actual_retain = env.sessions.retain_continuation_provider_response

    async def retain(*, binding, chunk, finished_at, required_work):
        await actual_retain(binding=binding, chunk=chunk, finished_at=finished_at, required_work=required_work)
        await cut.observe(env, sdk)

    monkeypatch.setattr(env.sessions, "retain_continuation_provider_response", retain)
    actual_advance = env.composer.advance_continuation

    async def advance(
        *, checkpoint_id, stack, chunks, session_operation_context,
        required_work, provider_owner, progress, operation_authority,
    ):
        assert operation_authority is env.authority
        advanced = await actual_advance(
            checkpoint_id=checkpoint_id, stack=stack, chunks=chunks,
            session_operation_context=session_operation_context,
            required_work=required_work, provider_owner=provider_owner, progress=progress,
            operation_authority=operation_authority,
        )
        await cut.observe(env, sdk)
        return advanced

    monkeypatch.setattr(env.composer, "advance_continuation", advance)
    if cut.name == "p4_retained":
        actual_persist = env.sessions.persist_compose_turn

        def persist(*args, **kwargs):
            actual_receipt = actual_persist(*args, **kwargs)
            observe_after_required_sql()
            return actual_receipt

        monkeypatch.setattr(env.sessions, "persist_compose_turn", persist)
    if cut.name == "publication":
        from elspeth.web.required_work import RequiredWorkSource
        actual_terminal_sql = env.sessions._run_composer_terminal_sql

        async def terminal_sql(running, func, *, can_retry=None, required_work=None,
                               terminal_source=RequiredWorkSource.OPERATION_TERMINAL_SQL):
            def observed_terminal():
                actual_terminal = func()
                observe_after_required_sql()
                return actual_terminal
            return await actual_terminal_sql(running, observed_terminal, can_retry=can_retry,
                required_work=required_work, terminal_source=terminal_source)

        monkeypatch.setattr(env.sessions, "_run_composer_terminal_sql", terminal_sql)


def driver_worker(env: DriverEnvironment) -> ComposerAsyncWorker:
    draining = threading.Event()
    worker = ComposerAsyncWorker(
        app=env.app, authority=env.authority, concurrency=1, scan_interval_seconds=0.01,
        claim_lease_seconds=30, drain_seconds=0.1, owner_instance_id="test-owner",
        process_recovery=ProcessRecovery(watchdog=OwnedTestProcessWatchdog(draining), instance_draining=draining),
        instance_draining=draining,
    )
    env.app.state.composer_async_worker = worker
    return worker


def failure_objects(root: BaseException) -> tuple[BaseException, ...]:
    seen: list[BaseException] = []
    pending = [root]
    while pending:
        item = pending.pop()
        if any(item is old for old in seen):
            continue
        seen.append(item)
        if isinstance(item, BaseExceptionGroup):
            pending.extend(item.exceptions)
        if item.__cause__ is not None:
            pending.append(item.__cause__)
        if item.__context__ is not None:
            pending.append(item.__context__)
    return tuple(seen)


@dataclass(frozen=True, slots=True)
class RetiredAttempt:
    sequence: int
    generation: int
    fence_operation_id: str
    fence_epoch: int
    selected: SelectedDriverState | None


async def run_driver_attempt(
    env: DriverEnvironment, sdk: DeterministicDriverSDK, monkeypatch,
    *, cut: DriverCutObservation | None = None,
    interrupt: bool = True,
    expect_suspension: bool = False,
    configure_executor: bool = False,
) -> RetiredAttempt:
    worker = driver_worker(env)
    if configure_executor:
        from elspeth.web.async_workers import configure_required_executor_recovery

        configure_required_executor_recovery(
            drain_seconds=1.0, instance_draining=worker._instance_draining,
            generation_unavailable=threading.Event(),
            recovery_callback=worker._process_recovery.required_generation_expired,
        )
    actual_resolve = env.authority.resolve_continuation_frontier_evidence

    def resolve(running, *, expected_checkpoint_id, expected_cursor):
        actual = actual_resolve(
            running, expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor,
        )
        env.evidence_observations.append(actual)
        return actual

    monkeypatch.setattr(env.authority, "resolve_continuation_frontier_evidence", resolve)
    if cut is not None:
        observe_real_driver(env, sdk, cut, monkeypatch)
    deadline = asyncio.get_running_loop().time() + 10
    claim = None
    while claim is None:
        claims = env.authority.claim_segment(limit=1)
        if claims:
            (claim,) = claims
            break
        assert asyncio.get_running_loop().time() < deadline, "Actual SQL eligibility did not become claimable"
        await asyncio.sleep(0.02)
    assert claim.session_id == env.case.session_id and claim.operation_id == env.case.operation_id
    key = (claim.session_id, claim.operation_id)
    worker._continuation_claims[key] = claim
    worker._local_cancels[key] = asyncio.Event()
    task = asyncio.create_task(worker._job(claim), name="test-real-continuation-driver")
    worker._jobs[key] = task
    marker = object()
    try:
        if cut is not None:
            reached = asyncio.create_task(cut.reached.wait(), name="test-driver-cut-observer")
            try:
                done, _pending = await asyncio.wait((task, reached), timeout=30, return_when=asyncio.FIRST_COMPLETED)
                assert reached in done and cut.selected is not None, f"Driver never reached actual cut {cut.name}"
                assert not task.done(), "An already finished task is not an interrupted owner"
                if interrupt:
                    task.cancel(marker)
                    await asyncio.sleep(0)
                cut.release.set()  # The real required child finishes through its existing owner.
            finally:
                if not reached.done():
                    reached.cancel()
                await asyncio.gather(reached, return_exceptions=True)
            if interrupt:
                with pytest.raises(BaseException) as caught:
                    await asyncio.wait_for(asyncio.shield(task), timeout=15)
                assert task.done(), "A waiter timeout does not prove the actual owner stopped"
                assert any(isinstance(item, asyncio.CancelledError) and item.args == (marker,)
                           for item in failure_objects(caught.value))
            else:
                await asyncio.wait_for(asyncio.shield(task), timeout=30)
        else:
            if expect_suspension:
                with pytest.raises(BaseException) as suspended:
                    await asyncio.wait_for(asyncio.shield(task), timeout=30)
                assert task.done(), "A waiter timeout is not actual recovery evidence"
            else:
                await asyncio.wait_for(asyncio.shield(task), timeout=30)
        coordinator = worker._required_coordinators[key]
        coordinator.assert_completed()
        for recovery in worker._recovery_coordinators.get(key, []):
            recovery.assert_completed()
        proof = worker._continuation_outcomes[key]
        if expect_suspension or (cut is not None and interrupt and cut.name != "publication"):
            env.authority.verify_recovery(proof.receipt)
            assert proof.receipt.sequence == claim.segment_sequence
            assert proof.receipt.generation == claim.ownership_generation + 1
            if expect_suspension:
                assert proof.original is suspended.value
        for lease in worker._continuation_adoptions[key]:
            assert lease.closed and not lease._owned_tasks
            assert lease._renewal_task is not None and lease._renewal_task.done()
            assert lease._close_task is not None and lease._close_task.done()
        with env.sessions._engine.connect() as conn:
            history = conn.execute(select(composer_segment_attempts_table).where(
                composer_segment_attempts_table.c.session_id == str(claim.session_id),
                composer_segment_attempts_table.c.operation_id == claim.operation_id,
                composer_segment_attempts_table.c.sequence == claim.segment_sequence,
                composer_segment_attempts_table.c.generation == claim.ownership_generation,
                composer_segment_attempts_table.c.claim_attempt == claim.attempt,
            )).one()
        assert history.revoked_at is not None
        lock = await composer_session_lock_registry(env.app).get_lock(str(env.case.session_id))
        assert not lock.locked(), "Process lock may release only after actual SQL disposition readback"
        retired = RetiredAttempt(
            claim.segment_sequence, claim.ownership_generation,
            history.session_operation_id, history.session_operation_epoch,
            None if cut is None else cut.selected,
        )
        worker._job_finished(key, task)
        assert not worker._jobs and key not in worker._continuation_claims
        assert key not in worker._required_coordinators and key not in worker._continuation_adoptions
        worker._stopping = True
        worker.assert_shutdown_complete()
        return retired
    finally:
        if cut is not None:
            cut.release.set()
        if not task.done():
            task.cancel("test-owned cleanup")
            await asyncio.gather(task, return_exceptions=True)
        await worker._process_recovery.join_escalation()
```

`RetiredAttempt` retains only primitive historical identity and the selected
private bytes; no service, `TurnEvidence`, transcript owner, recorder, lease,
coordinator, task or SDK custody survives into the next attempt. The new
environment reconstructs **all** of those from current owners and durable
bytes. The deterministic SDK script is external to the application, so its
counter intentionally survives a restart and detects repeated paid calls.

The P4 and terminal observers are bound to the actual existing
`persist_compose_turn_async` and `complete_composer_async_operation` methods
above. Their returned receipt is unchanged. When those calls run inside a
lease-owned child, cancellation must first wait for that child; the test
releases its own observation barrier after delivery and then verifies the
actual required-work/SQL outcome. This is cooperative cancellation evidence,
not process-death evidence. D6 handles a child that never runs its finally
blocks after the durable boundary.

## D4: Compare actual SQL relationships and unchanged user/model text

**Append:** `tests/helpers/continuation_driver.py`.
Do not remove UUID-looking strings, numbers, timestamps embedded in authored
text, native signatures, transcript entries or tool argument fields. The
only alpha-binding below is an explicit SQL reference **column** resolved
against the actual same-session state/message table. Full provider text is
compared without any alpha transformation at the next SDK boundary.

- [ ] Add this complete outcome reader. It checks real P4/graph/proposal/tool
  pairings and actual SDK/audit/attempt/ledger joins. It compares model and
  user values unchanged. It intentionally excludes physical generation and
  wall-clock duration from semantic equality while proving that every real
  physical row has an actual historical owner.

```python
from elspeth.web.composer.continuation_codec import decode_frontier_chunk


@dataclass(frozen=True, slots=True)
class ObservedDriverOutcome:
    original_user_id: str
    conversation_json: str
    graphs_json: str
    proposals_json: str
    interpretations_json: str
    usage_json: str
    effects_json: str
    obligations_json: str
    evidence_json: str
    actions_json: str
    final_message: str


def read_driver_outcome(
    env: DriverEnvironment, sdk: DeterministicDriverSDK, *, terminal_replay: bool = False,
) -> ObservedDriverOutcome:
    sid = str(env.case.session_id)
    with env.sessions._engine.connect() as conn, conn.begin():
        messages = conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == sid)
                                .order_by(chat_messages_table.c.sequence_no)).all()
        states = conn.execute(select(composition_states_table).where(composition_states_table.c.session_id == sid)
                              .order_by(composition_states_table.c.version)).all()
        proposals = conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.session_id == sid)
                                 .order_by(composition_proposals_table.c.tool_call_id)).all()
        interpretations = conn.execute(select(interpretation_events_table).where(interpretation_events_table.c.session_id == sid)
                                       .order_by(interpretation_events_table.c.tool_call_id)).all()
        attempts = conn.execute(select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == sid)).all()
        ledgers = conn.execute(select(token_usage_ledger_table).where(token_usage_ledger_table.c.session_id == sid)).all()
        effects = conn.execute(select(composer_continuation_effects_table).where(
            composer_continuation_effects_table.c.session_id == sid,
            composer_continuation_effects_table.c.operation_id == env.case.operation_id,
        ).order_by(composer_continuation_effects_table.c.effect_key)).all()
        history = conn.execute(select(composer_segment_attempts_table).where(
            composer_segment_attempts_table.c.session_id == sid,
            composer_segment_attempts_table.c.operation_id == env.case.operation_id,
        )).all()
    terminal = env.authority.get(session_id=env.case.session_id, operation_id=env.case.operation_id)
    assert terminal is not None and terminal.status == "completed" and terminal.result_json is not None
    selected = select_driver_state(env)
    stack = decode_continuation_snapshot(selected.payload_json)
    assert not stack.pending_settlement_ids and not stack.pending_owner_results
    assert all(item.disposition in ("committed", "refused") for item in stack.effect_frontier)
    assert all(row.revoked_at is not None and row.outcome is not None for row in history)
    historical_fences = {(row.session_operation_id, row.session_operation_epoch, row.session_operation_lease_token) for row in history}
    historical_creators = {(row.sequence, row.generation) for row in history}
    state_versions = {row.id: row.version for row in states}
    assert len(state_versions) == len(states)
    assert len({row.version for row in states}) == len(states)

    def state_ref(value: str | None) -> int | None:
        if value is None:
            return None
        assert value in state_versions, "A graph reference must identify one actual same-session state"
        return state_versions[value]

    conversation = [row for row in messages if row.role in ("user", "assistant", "tool")]
    message_positions = {row.id: index for index, row in enumerate(conversation)}
    assert len(message_positions) == len(conversation)
    actual_user = [row for row in conversation if row.role == "user"]
    assert len(actual_user) == 1
    assert actual_user[0].id == env.case.original_user_message_id
    assert actual_user[0].content == REQUEST
    assert actual_user[0].operation_id == env.case.operation_id
    assert actual_user[0].writer_principal == "route_user_message"
    request_calls: dict[str, tuple[str, dict[str, Any]]] = {}
    tool_rows = []
    for row in conversation:
        if row.role == "assistant" and row.tool_calls:
            for call in row.tool_calls:
                assert call["id"] not in request_calls, "An issued tool call cannot be appended twice"
                request_calls[call["id"]] = (row.id, call)
        if row.role == "tool":
            assert row.tool_call_id in request_calls
            assistant_id, _call = request_calls[row.tool_call_id]
            assert row.parent_assistant_id == assistant_id
            assert message_positions[assistant_id] < message_positions[row.id]
            tool_rows.append(row)
    assert len({row.tool_call_id for row in tool_rows}) == len(tool_rows)
    assert {row.tool_call_id for row in tool_rows} == set(request_calls)
    graph_values = [
        {
            "version": row.version, "sources": row.sources, "nodes": row.nodes,
            "edges": row.edges, "outputs": row.outputs, "metadata": row.metadata_,
            "is_valid": row.is_valid, "validation_errors": row.validation_errors,
            "provenance": row.provenance,
        }
        for row in states
    ]
    conversation_values = [
        {
            "role": row.role, "content": row.content, "raw_content": row.raw_content,
            "tool_calls": row.tool_calls, "tool_call_id": row.tool_call_id,
            "parent_position": None if row.parent_assistant_id is None else message_positions[row.parent_assistant_id],
            "state_version": state_ref(row.composition_state_id),
            "writer_principal": row.writer_principal,
        }
        for row in conversation
    ]
    assert len({row.tool_call_id for row in proposals}) == len(proposals)
    proposal_values = [
        {
            "call": row.tool_call_id, "tool": row.tool_name, "status": row.status,
            "summary": row.summary, "rationale": row.rationale, "affects": row.affects,
            "arguments": row.arguments_json, "redacted": row.arguments_redacted_json,
            "base": state_ref(row.base_state_id), "committed": state_ref(row.committed_state_id),
            "original_user": row.user_message_id, "arguments_hash": row.tool_arguments_hash,
            "model": row.composer_model_identifier, "model_version": row.composer_model_version,
            "provider": row.composer_provider, "skill_hash": row.composer_skill_hash,
        }
        for row in proposals
    ]
    interpretation_values = [
        {
            "call": row.tool_call_id, "state": state_ref(row.composition_state_id),
            "node": row.affected_node_id, "term": row.user_term, "kind": row.kind,
            "draft": row.llm_draft, "accepted": row.accepted_value, "choice": row.choice,
            "source": row.interpretation_source, "actor": row.actor,
            "model": row.model_identifier, "model_version": row.model_version,
            "provider": row.provider, "skill_hash": row.composer_skill_hash,
            "arguments_hash": row.arguments_hash, "hash_domain": row.hash_domain_version,
        }
        for row in interpretations
    ]
    calls = [
        envelope["call"] for row in messages if row.role == "audit"
        for envelope in (row.tool_calls or ()) if envelope["_kind"] == "llm_call_audit"
    ]
    calls_by_id = {call["call_id"]: call for call in calls}
    assert len(calls_by_id) == len(calls) == len(attempts) == len(sdk.invocations)
    attempt_by_id = {row.attempt_id: row for row in attempts}
    ledger_by_id = {row.entry_id: row for row in ledgers}
    assert set(calls_by_id) == set(attempt_by_id)
    assert len(ledgers) == len(attempts) == len(ledger_by_id)
    sdk_by_response = {f"sdk-{item.role}-{item.role_ordinal}": item for item in sdk.invocations}
    usage_values = []
    attempt_labels: dict[str, str] = {}
    for attempt_id, call in calls_by_id.items():
        invocation = sdk_by_response[call["provider_request_id"]]
        attempt = attempt_by_id[attempt_id]
        assert (attempt.operation_id, attempt.operation_epoch, attempt.lease_token) in historical_fences
        assert attempt.settled_at is not None and attempt.ledger_entry_id in ledger_by_id
        ledger = ledger_by_id[attempt.ledger_entry_id]
        assert ledger.identity_id == "alice" and ledger.session_id == sid
        expected = (17 + invocation.role_ordinal, 11 + invocation.role_ordinal, 3, 2)
        assert (ledger.prompt_tokens, ledger.completion_tokens, ledger.cached_prompt_tokens, ledger.reasoning_tokens) == expected
        assert (call["prompt_tokens"], call["completion_tokens"], call["cached_prompt_tokens"], call["reasoning_tokens"]) == expected
        label = f"{invocation.role}:{invocation.role_ordinal}"
        assert label not in attempt_labels.values()
        attempt_labels[attempt_id] = label
        usage_values.append({"call": label, "usage": expected, "source": ledger.source, "model": ledger.model})
    assert len(attempt_labels) == len(sdk.invocations)
    effect_values = []
    for effect in effects:
        assert (effect.creator_sequence, effect.creator_generation) in historical_creators
        assert effect.disposition in ("committed", "refused")
        assert effect.receipt_key is not None and effect.result_chunk_key is not None
        if effect.provider_attempt_id is not None:
            assert effect.provider_attempt_id in attempt_labels
            assert (effect.physical_sequence, effect.physical_generation) in historical_creators
        committed = None if effect.committed_state_ids_json is None else [state_ref(value) for value in json.loads(effect.committed_state_ids_json)]
        effect_values.append({
            "key": effect.effect_key, "owner": effect.owner, "disposition": effect.disposition,
            "physical": None if effect.provider_attempt_id is None else attempt_labels[effect.provider_attempt_id],
            "states": committed, "graph_sha256": effect.graph_sha256,
        })
    assert stack.semantic_frontier is not None
    with env.sessions._engine.connect() as conn:
        stored = load_snapshot(conn, session_id=sid, operation_id=env.case.operation_id)
        actual_chunks = load_private_chunks(conn, session_id=sid, operation_id=env.case.operation_id, stack=stack)
    assert stored.payload_json == selected.payload_json
    frontier_chunk = next(chunk for chunk in actual_chunks if chunk.reference == stack.semantic_frontier)
    frontier = decode_frontier_chunk(frontier_chunk)
    assert frontier.obligations and frontier.obligations[0].obligation_id == "request"
    assert all(item.original_message_id == env.case.original_user_message_id for item in frontier.obligations)
    assert all(item.status in ("satisfied", "blocked") for item in frontier.obligations)
    assert not frontier.pending_review_bindings and stack.active_action_json is None
    if terminal_replay:
        # Only immutable public replay uses its already committed as-of-terminal
        # root. This value is not authorization for any resumed business action.
        assert not env.evidence_observations
        assert env.authority.get(session_id=env.case.session_id, operation_id=env.case.operation_id) == terminal
        actual_evidence = frontier.evidence
    else:
        assert env.evidence_observations, "The actual SQL evidence resolver must precede terminal END"
        actual_evidence = env.evidence_observations[-1]
    assert all(type(item) is ContinuationEvidenceRef for item in actual_evidence)
    assert len({item.receipt_id for item in actual_evidence}) == len(actual_evidence)
    assert {item.receipt_id: item for item in frontier.evidence} == {item.receipt_id: item for item in actual_evidence}
    effects_by_key = {item.effect_key: item for item in effects}
    for item in actual_evidence:
        if item.receipt_id.startswith("base:"):
            assert item.receipt_id == "base:" + str(env.case.base_state_id)
            assert item.source_kind == "graph"
        else:
            assert item.receipt_id in effects_by_key
            assert effects_by_key[item.receipt_id].disposition in ("committed", "refused")
    receipt_ids = {item.receipt_key for item in effects} | set(attempt_labels) | set(ledger_by_id)
    assert all(item.owner_receipt_id in receipt_ids for item in frontier.action_receipts)
    assert all(item.disposition in ("committed", "refused") for item in frontier.action_receipts)
    obligations = [
        (item.obligation_id, item.parent_id, item.description, item.original_message_id,
         item.start, item.end, item.status)
        for item in frontier.obligations
    ]
    evidence_semantics = sorted(
        (item.receipt_id, item.source_kind, item.content_hash, item.coverage_binding)
        for item in actual_evidence if item.source_kind != "provider"
    )
    action_semantics = [(item.action_key, item.disposition) for item in frontier.action_receipts]
    assistant = [row for row in conversation if row.role == "assistant" and not row.tool_calls]
    assert assistant
    return ObservedDriverOutcome(
        str(actual_user[0].id), canonical_json(conversation_values), canonical_json(graph_values),
        canonical_json(proposal_values), canonical_json(interpretation_values),
        canonical_json(sorted(usage_values, key=lambda item: item["call"])),
        canonical_json(effect_values), canonical_json(obligations),
        canonical_json(evidence_semantics), canonical_json(action_semantics), assistant[-1].content,
    )


def assert_same_driver_outcome(baseline: ObservedDriverOutcome, resumed: ObservedDriverOutcome) -> None:
    assert resumed == baseline


def assert_next_sdk_wire(baseline: DeterministicDriverSDK, resumed: DeterministicDriverSDK, cut: DriverCutObservation) -> None:
    prefix = len(cut.sdk_prefix)
    assert tuple(baseline.invocations[:prefix]) == tuple(resumed.invocations[:prefix]) == cut.sdk_prefix
    assert len(baseline.invocations) > prefix and len(resumed.invocations) > prefix
    assert resumed.invocations[prefix] == baseline.invocations[prefix]
```

`assert_next_sdk_wire` compares the entire safe SDK kwargs JSON, including
`api_base`, model/custom provider, ordered native blocks, role/content,
tools/tool arguments and message order. The two branches started from the
same actual checkpoint, so historical nominal IDs are already identical.
The semantic comparison does **not** claim byte equality of the entire
suffix after independently generated post-cut state/proposal IDs. All
user/model strings are unchanged; only explicitly selected SQL reference
columns become actual state versions or parent positions.

## D5: Run the uninterrupted and cold branches through actual owners

**Append:** `tests/helpers/continuation_driver.py` and create the exact test
module below. The copied SQL fence is released through the actual repository
and then recovered through its exact-generation port. This models a clean
owner exit in an independent copied store. D6's real spawned-child case is
the separate hard-death claim; a canceled Task is never used as its proof.

- [ ] Add this complete copied-owner transition and branch driver:

```python
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence


def recover_copied_driver_owner(env: DriverEnvironment) -> tuple[int, int]:
    record = env.authority.get(session_id=env.case.session_id, operation_id=env.case.operation_id)
    assert record is not None and record.lifecycle_version == 2
    with env.sessions._engine.connect() as conn:
        history = conn.execute(select(composer_segment_attempts_table).where(
            composer_segment_attempts_table.c.session_id == str(env.case.session_id),
            composer_segment_attempts_table.c.operation_id == env.case.operation_id,
            composer_segment_attempts_table.c.sequence == record.current_segment_sequence,
            composer_segment_attempts_table.c.generation == record.ownership_generation,
        )).one()
    copied_context = SessionOperationContext(
        fence=SessionOperationFence(
            session_id=str(env.case.session_id), operation_id=history.session_operation_id,
            lease_token=history.session_operation_lease_token, operation_epoch=history.session_operation_epoch,
        ),
        operation_kind=SessionOperationKind.COMPOSE,
    )
    repository = env.sessions.session_operation_authority
    repository.release(copied_context)
    if record.status == "completed":
        return history.sequence, history.generation
    assert record.status == "running"
    assert repository.recover_composer_segment(
        session_id=env.case.session_id, operation_id=env.case.operation_id,
        expected_generation=record.ownership_generation, actor_user_id="alice", auth_provider_type="local",
    ) is True
    changed = env.authority.get(session_id=env.case.session_id, operation_id=env.case.operation_id)
    assert changed.current_segment_sequence == history.sequence
    assert changed.ownership_generation == history.generation + 1
    return history.sequence, history.generation


@dataclass(frozen=True, slots=True)
class DriverComparison:
    baseline: ObservedDriverOutcome
    resumed: ObservedDriverOutcome
    baseline_sdk: DeterministicDriverSDK
    resumed_sdk: DeterministicDriverSDK
    cut: DriverCutObservation
    retired: tuple[RetiredAttempt, ...]
    cold_database_url: str
    case: AdmittedDriverCase


async def compare_driver_cut(
    operation_store, tmp_path: Path, monkeypatch, name: CutName,
    *, early: bool = False, native: bool = False, repair: bool = False, second_cut: CutName | None = None,
) -> DriverComparison:
    case = await seed_driver_case(operation_store, tmp_path / "data", empty=early)
    baseline_url = clone_admitted_database(case, tmp_path / "baseline.db")
    cold_path = tmp_path / "cold.db"
    cut = DriverCutObservation(name, copy_to=cold_path)
    assert not repair or not (early or native)
    baseline_sdk = repair_driver_script(case) if repair else ordinary_driver_script(case, early=early, native=native)
    baseline_env = fresh_driver_environment(baseline_url, case)
    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(litellm, "acompletion", baseline_sdk.complete)
            baseline_retired = await run_driver_attempt(
                baseline_env, baseline_sdk, patcher, cut=cut, interrupt=False,
            )
        baseline_sdk.assert_consumed()
        baseline_outcome = read_driver_outcome(baseline_env, baseline_sdk)
    finally:
        baseline_env.dispose()
    assert cut.selected is not None and cold_path.exists()
    cold_sdk = DeterministicDriverSDK(
        replies=(repair_driver_script(case) if repair else ordinary_driver_script(case, early=early, native=native)).replies,
        consumed=dict(cut.sdk_consumed), invocations=list(cut.sdk_prefix),
    )
    cold_url = f"sqlite:///{cold_path}"
    cold_env = fresh_driver_environment(cold_url, case)
    retired: list[RetiredAttempt] = []
    try:
        copied = select_driver_state(cold_env)
        assert copied == cut.selected, "The cold branch must begin at the complete actual selected bytes"
        copied_sequence, copied_generation = recover_copied_driver_owner(cold_env)
        if name == "publication":
            before = cold_env.authority.get(session_id=case.session_id, operation_id=case.operation_id)
            assert before.status == "completed"
            assert cold_env.authority.claim_segment(limit=1) == ()
            assert select_driver_state(cold_env).payload_json == copied.payload_json
            assert cold_sdk.invocations == list(cut.sdk_prefix)
        else:
            with monkeypatch.context() as patcher:
                patcher.setattr(litellm, "acompletion", cold_sdk.complete)
                if second_cut is None:
                    attempt = await run_driver_attempt(cold_env, cold_sdk, patcher)
                    retired.append(attempt)
                else:
                    attempt = await run_driver_attempt(
                        cold_env, cold_sdk, patcher, cut=DriverCutObservation(second_cut),
                    )
                    retired.append(attempt)
            assert attempt.sequence == copied_sequence
            assert attempt.generation == copied_generation + 1
            assert attempt.fence_operation_id != baseline_retired.fence_operation_id
            if second_cut is not None:
                cold_env.dispose()
                del cold_env
                # A genuinely new service, gateway, SQL reader, lease, coordinator,
                # evidence owner, recorder and transcript are constructed here.
                cold_env = fresh_driver_environment(cold_url, case)
                with monkeypatch.context() as patcher:
                    patcher.setattr(litellm, "acompletion", cold_sdk.complete)
                    restarted = await run_driver_attempt(cold_env, cold_sdk, patcher)
                    retired.append(restarted)
                assert restarted.sequence == attempt.sequence
                assert restarted.generation == attempt.generation + 1
                assert restarted.fence_operation_id != attempt.fence_operation_id
                assert restarted.fence_epoch > attempt.fence_epoch
        cold_sdk.assert_consumed()
        resumed_outcome = read_driver_outcome(cold_env, cold_sdk, terminal_replay=name == "publication")
        assert_same_driver_outcome(baseline_outcome, resumed_outcome)
        if name != "publication":
            assert_next_sdk_wire(baseline_sdk, cold_sdk, cut)
        else:
            assert baseline_sdk.invocations == cold_sdk.invocations
        return DriverComparison(
            baseline_outcome, resumed_outcome, baseline_sdk, cold_sdk, cut, tuple(retired), cold_url, case,
        )
    finally:
        cold_env.dispose()
```

- [ ] Create this complete integration module. Each case owns its actual
  SQLite clone, native receipts and SDK counter. The assertion of nonempty
  generated graph history is independent of the final message text.

```python
from __future__ import annotations

from dataclasses import replace

import pytest

from tests.helpers.continuation_driver import (
    assert_same_driver_outcome, compare_driver_cut,
)
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", (
    "ingress", "primary_response", "partial_batch", "manual_response",
    "end_response", "p4_retained", "publication",
))
async def test_actual_driver_cold_cut_matches_uninterrupted_next_wire_and_sql(
    operation_store, tmp_path, monkeypatch, cut,
):
    compared = await compare_driver_cut(operation_store, tmp_path, monkeypatch, cut)
    assert compared.cut.selected is not None
    assert "Final exact café description" in compared.resumed.graphs_json
    assert "author:initial" in compared.resumed.conversation_json
    assert "author:suffix" in compared.resumed.conversation_json
    assert len(compared.resumed_sdk.invocations) == len(compared.baseline_sdk.invocations)


@pytest.mark.asyncio
async def test_actual_early_checkpoint_response_resumes_without_another_review(
    operation_store, tmp_path, monkeypatch,
):
    compared = await compare_driver_cut(operation_store, tmp_path, monkeypatch, "early_response", early=True)
    assert sum(item.role == "early" for item in compared.resumed_sdk.invocations) == 1
    assert compared.resumed.usage_json == compared.baseline.usage_json


@pytest.mark.asyncio
async def test_two_fresh_owners_keep_the_original_operation_and_complete_debt(
    operation_store, tmp_path, monkeypatch,
):
    compared = await compare_driver_cut(
        operation_store, tmp_path, monkeypatch, "ingress", second_cut="manual_response",
    )
    assert len(compared.retired) == 2
    first, second = compared.retired
    assert second.sequence == first.sequence and second.generation == first.generation + 1
    assert compared.resumed.original_user_id == compared.baseline.original_user_id
    assert compared.resumed.obligations_json == compared.baseline.obligations_json


@pytest.mark.asyncio
async def test_real_end_withhold_and_primary_repair_resume_before_fresh_admission(
    operation_store, tmp_path, monkeypatch,
):
    compared = await compare_driver_cut(operation_store, tmp_path, monkeypatch, "repair", repair=True)
    assert sum(item.role == "end" for item in compared.resumed_sdk.invocations) == 2
    assert sum(item.role == "semantic" for item in compared.resumed_sdk.invocations) == 1
    assert "Repaired exact café description" in compared.resumed.graphs_json
    assert "author:repair" in compared.resumed.conversation_json


@pytest.mark.asyncio
async def test_native_blocks_preserve_full_tail_signatures_order_and_exact_next_wire(
    operation_store, tmp_path, monkeypatch,
):
    compared = await compare_driver_cut(operation_store, tmp_path, monkeypatch, "primary_response", native=True)
    prefix = len(compared.cut.sdk_prefix)
    assert compared.baseline_sdk.invocations[prefix].wire_json == compared.resumed_sdk.invocations[prefix].wire_json
    assert "opaque-signature-A" in compared.resumed_sdk.invocations[prefix].wire_json
    assert "opaque-redacted-B" in compared.resumed_sdk.invocations[prefix].wire_json
    assert "opaque-signature-C" in compared.resumed_sdk.invocations[prefix].wire_json
    assert "native tail" in compared.resumed_sdk.invocations[prefix].wire_json


@pytest.mark.asyncio
async def test_outcome_oracle_rejects_changed_graph_and_uuid_looking_user_value(
    operation_store, tmp_path, monkeypatch,
):
    compared = await compare_driver_cut(operation_store, tmp_path, monkeypatch, "partial_batch")
    with pytest.raises(AssertionError):
        assert_same_driver_outcome(compared.baseline, replace(
            compared.resumed,
            graphs_json=compared.resumed.graphs_json.replace("Final exact café description", "Changed description"),
        ))
    assert "00000000-0000-4000-8000-000000009999" in compared.resumed.conversation_json
    with pytest.raises(AssertionError):
        assert_same_driver_outcome(compared.baseline, replace(
            compared.resumed,
            conversation_json=compared.resumed.conversation_json.replace(
                "00000000-0000-4000-8000-000000009999", "00000000-0000-4000-8000-000000008888",
            ),
        ))
```

The native test depends on the concrete T6–T10 current-use implementation.
A missing owner must fail/refuse
before further provider work, with the retained bytes available for safe
recovery. Do not claim this test is GREEN until that planned API and
its complete-value inspection route are concretely integrated.

## D6: Prove hard death with the existing spawned-child/SQLite pattern

**Create:** `tests/integration/web/composer/test_continuation_driver_process_death.py`.
**Existing pattern:** `tests/integration/web/test_blob_replacement_process_death.py`
uses `multiprocessing.get_context("spawn").Process`, a lane-owned SQLite
database, an exit at an actual committed owner boundary, and cleanup limited
to the exact child it started. Reuse that pattern below; do not signal any
service, foreign PID, sibling suite or watchdog target.

- [ ] Add this complete child and hard-death packet. Its SDK JSONL file is
  the deterministic external fake's ordinary request output, fsynced before
  the deliberate child exit. It records actual fake-SDK entry independently
  of the application's markers. It is never read by production code and
  grants no recovery authority.

```python
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import time
from pathlib import Path

import litellm
import pytest
from sqlalchemy import select

from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.sessions.models import (
    composer_continuation_effects_table as effects,
    composer_segment_attempts_table as history,
    quota_provider_attempts_table as attempts,
    token_usage_ledger_table as ledger,
)
from tests.helpers.continuation_driver import (
    AdmittedDriverCase, DeterministicDriverSDK, DriverCutObservation, SDKInvocation,
    clone_admitted_database, fresh_driver_environment, matches_cut,
    ordinary_driver_script, run_driver_attempt, seed_driver_case, select_driver_state,
)
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as operation_store


class DyingDriverCut(DriverCutObservation):
    async def observe(self, env, sdk) -> None:
        selected = select_driver_state(env)
        if matches_cut(self.name, selected, sdk):
            os._exit(42)


def _append_sdk_entry(path: Path, invocation: SDKInvocation) -> None:
    value = {
        "role": invocation.role, "role_ordinal": invocation.role_ordinal,
        "wire_json": invocation.wire_json, "response_content": invocation.response_content,
    }
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())


def _die_in_actual_driver(database_url: str, case: AdmittedDriverCase, mode: str, sdk_path: Path) -> None:
    async def run() -> None:
        env = fresh_driver_environment(database_url, case, lease_seconds=2)
        sdk = ordinary_driver_script(case)
        with pytest.MonkeyPatch.context() as patcher:
            async def complete(**kwargs):
                response = await sdk.complete(**kwargs)
                _append_sdk_entry(sdk_path, sdk.invocations[-1])
                if mode == "sdk_unknown":
                    os._exit(42)  # Actual SDK entered; no response was delivered to ELSPETH.
                return response

            patcher.setattr(litellm, "acompletion", complete)
            if mode == "admitted_no_sdk":
                actual_admit = ProviderCallCustody.admit

                async def admit(self, *, model):
                    await actual_admit(self, model=model)
                    os._exit(42)  # Actual quota admission committed, before mark_sdk_entered.

                patcher.setattr(ProviderCallCustody, "admit", admit)
            if mode == "response_retained":
                actual_retain = env.sessions.retain_continuation_provider_response

                async def retain(*, binding, chunk, finished_at, required_work):
                    await actual_retain(binding=binding, chunk=chunk, finished_at=finished_at, required_work=required_work)
                    os._exit(42)  # Complete actual response committed; normal finally never ran.

                patcher.setattr(env.sessions, "retain_continuation_provider_response", retain)
            cut = DyingDriverCut(mode) if mode in ("ingress", "p4_retained") else None
            await run_driver_attempt(env, sdk, patcher, cut=cut, interrupt=False, configure_executor=True)
        raise AssertionError("The child never reached its requested actual durable boundary")

    asyncio.run(run())


def _joined_owned_child(database_url: str, case: AdmittedDriverCase, mode: str, sdk_path: Path) -> None:
    process = multiprocessing.get_context("spawn").Process(
        target=_die_in_actual_driver, args=(database_url, case, mode, sdk_path),
    )
    process.start()
    try:
        process.join(timeout=40)
        assert not process.is_alive(), "The owned child did not reach its exact boundary"
        assert process.exitcode == 42
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
        process.close()


def _read_sdk_entries(path: Path) -> list[SDKInvocation]:
    if not path.exists():
        return []
    return [SDKInvocation(**json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines()]


async def _recover_actual_expired_child(env) -> tuple[int, int]:
    before = env.authority.get(session_id=env.case.session_id, operation_id=env.case.operation_id)
    assert before is not None and before.status == "running"
    repository = env.sessions.session_operation_authority
    deadline = time.monotonic() + 10
    while not repository.recover_composer_segment(
        session_id=env.case.session_id, operation_id=env.case.operation_id,
        expected_generation=before.ownership_generation, actor_user_id="alice", auth_provider_type="local",
    ):
        assert time.monotonic() < deadline, "The actual expired local owner was not recoverable"
        await asyncio.sleep(0.05)
    after = env.authority.get(session_id=env.case.session_id, operation_id=env.case.operation_id)
    assert after.current_segment_sequence == before.current_segment_sequence
    assert after.ownership_generation == before.ownership_generation + 1
    assert repository.recover_composer_segment(
        session_id=env.case.session_id, operation_id=env.case.operation_id,
        expected_generation=before.ownership_generation, actor_user_id="alice", auth_provider_type="local",
    ) is False
    with env.sessions._engine.connect() as conn:
        old = conn.execute(select(history).where(
            history.c.session_id == str(env.case.session_id), history.c.operation_id == env.case.operation_id,
            history.c.sequence == before.current_segment_sequence, history.c.generation == before.ownership_generation,
        )).one()
    assert old.revoked_at is not None and old.outcome == "interrupted"
    return old.sequence, old.generation


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("admitted_no_sdk", "sdk_unknown", "response_retained", "ingress", "p4_retained"))
async def test_actual_child_death_preserves_effect_and_truthful_accounting(
    operation_store, tmp_path, monkeypatch, mode,
):
    case = await seed_driver_case(operation_store, tmp_path / "data")
    database_url = clone_admitted_database(case, tmp_path / "child.db")
    sdk_path = tmp_path / "child-sdk.jsonl"
    _joined_owned_child(database_url, case, mode, sdk_path)
    prefix = _read_sdk_entries(sdk_path)
    if mode in ("admitted_no_sdk", "ingress"):
        assert prefix == []
    if mode in ("sdk_unknown", "response_retained"):
        assert len(prefix) == 1 and prefix[0].role == "primary"
    env = fresh_driver_environment(database_url, case)
    try:
        original_attempt_id = None
        original_audit = None
        if mode == "response_retained":
            from elspeth.web.composer.continuation import ComposeFrame
            from elspeth.web.composer.continuation_codec import PrivateChunk, decode_retained_physical_audit_call
            from elspeth.web.sessions.continuation import read_chunk
            with env.sessions._engine.connect() as conn:
                original_attempt_id = conn.execute(select(attempts.c.attempt_id).where(
                    attempts.c.session_id == str(case.session_id),
                )).scalar_one()
                selected = select_driver_state(env)
                before_root = decode_continuation_snapshot(selected.payload_json)
                compose = next(frame for frame in before_root.frames if type(frame) is ComposeFrame)
                assert compose.pending_response is not None
                ref = compose.pending_response
                encoded = read_chunk(conn, session_id=str(case.session_id), operation_id=case.operation_id,
                    key=f"value:{ref.codec}:{ref.digest}").decode("utf-8", errors="strict")
                original_audit = decode_retained_physical_audit_call(PrivateChunk(ref, encoded))
                assert str(original_audit.call_id) == original_attempt_id
        old_sequence, old_generation = await _recover_actual_expired_child(env)
        consumed = {}
        for item in prefix:
            consumed[item.role] = consumed.get(item.role, 0) + 1
        sdk = DeterministicDriverSDK(ordinary_driver_script(case).replies, consumed, list(prefix))
        with monkeypatch.context() as patcher:
            patcher.setattr(litellm, "acompletion", sdk.complete)
            retired = await run_driver_attempt(env, sdk, patcher, expect_suspension=mode == "sdk_unknown")
        assert retired.sequence == old_sequence and retired.generation == old_generation + 1
        with env.sessions._engine.connect() as conn:
            actual_attempts = conn.execute(select(attempts).where(attempts.c.session_id == str(case.session_id))).all()
            actual_ledger = conn.execute(select(ledger).where(ledger.c.session_id == str(case.session_id))).all()
            actual_effects = conn.execute(select(effects).where(effects.c.session_id == str(case.session_id),
                effects.c.operation_id == case.operation_id)).all()
        by_ledger = {row.entry_id: row for row in actual_ledger}
        assert len(actual_attempts) == len(actual_ledger) == len(by_ledger)
        assert all(row.settled_at is not None and row.ledger_entry_id in by_ledger for row in actual_attempts)
        if mode == "sdk_unknown":
            assert sdk.invocations == prefix, "A missing response must never be replayed as a new SDK call"
            assert len(actual_attempts) == 1
            unknown = by_ledger[actual_attempts[0].ledger_entry_id]
            assert (unknown.prompt_tokens, unknown.completion_tokens, unknown.cached_prompt_tokens, unknown.reasoning_tokens) == (None, None, None, None)
            assert any(row.disposition == "unknown" for row in actual_effects)
            current = env.authority.get(session_id=case.session_id, operation_id=case.operation_id)
            assert current.status == "running" and current.phase == "retry_waiting"
            stack = decode_continuation_snapshot(select_driver_state(env).payload_json)
            assert not stack.is_handoff_safe
        else:
            sdk.assert_consumed()
            final = env.authority.get(session_id=case.session_id, operation_id=case.operation_id)
            assert final.status == "completed"
            zero = [row for row in actual_ledger if (
                row.prompt_tokens, row.completion_tokens, row.cached_prompt_tokens, row.reasoning_tokens
            ) == (0, 0, 0, 0)]
            assert len(zero) == int(mode == "admitted_no_sdk")
            assert len(actual_attempts) == len(sdk.invocations) + int(mode == "admitted_no_sdk")
            assert all(row.prompt_tokens is not None and row.completion_tokens is not None for row in actual_ledger)
            if mode == "response_retained":
                assert sum(item.role == "primary" and item.role_ordinal == 0 for item in sdk.invocations) == 1
                original = next(row for row in actual_attempts if row.attempt_id == original_attempt_id)
                original_usage = by_ledger[original.ledger_entry_id]
                assert (original_usage.prompt_tokens, original_usage.completion_tokens) == (17, 11)
                from elspeth.web.composer.continuation_audit_buffer import decode_recorder_snapshot
                from elspeth.web.sessions.continuation import read_chunk
                final_root = decode_continuation_snapshot(select_driver_state(env).payload_json)
                final_compose = next(frame for frame in final_root.frames if type(frame) is ComposeFrame)
                with env.sessions._engine.connect() as conn:
                    ref = final_compose.audit_buffer
                    encoded = read_chunk(conn, session_id=str(case.session_id), operation_id=case.operation_id,
                        key=f"value:{ref.codec}:{ref.digest}").decode("utf-8", errors="strict")
                buffer = decode_recorder_snapshot(PrivateChunk(ref, encoded))
                recovered_calls = tuple(call for call in buffer.llm_calls if str(call.call_id) == original_attempt_id)
                assert recovered_calls == (original_audit,), "Cold restore must preserve the entire actual pre-finally call"
    finally:
        env.dispose()
```

The hard-death packet uses the actual unmodified local expiry predicate:
`SQLiteLocalSessionOperationAuthority._expired_owner_allows_takeover`
permits expired local ownership, while the live fence still refuses recovery.
It does not edit lease expiry, fake the database clock, release the dead
child's context to manufacture expiry, or claim PostgreSQL instance-membership
proof from SQLite. The storage companion's separate PostgreSQL contention
packet remains mandatory at Q.

## D7: Control the outcome oracle and reject changed physical configuration

**Append:** `tests/integration/web/composer/test_continuation_driver_equivalence.py`.
The database mutations below are deliberate negative controls in the exact
pytest-owned clone. Restore each changed value in `finally`. They are never
instructions to rewrite persisted production evidence.

- [ ] Add these complete controls. The first proves that a different real
  same-session graph reference is detected even though its FK still passes.
  The second proves that an ordinary UUID-looking user value is compared as
  text, not alpha-bound as an owned ID.

```python
from sqlalchemy import select, update

from elspeth.web.sessions.models import chat_messages_table
from tests.helpers.continuation_driver import fresh_driver_environment, read_driver_outcome


@pytest.mark.asyncio
async def test_oracle_rejects_changed_actual_tool_owner_binding(operation_store, tmp_path, monkeypatch):
    compared = await compare_driver_cut(operation_store, tmp_path, monkeypatch, "p4_retained")
    env = fresh_driver_environment(compared.cold_database_url, compared.case)
    target = None
    try:
        assert_same_driver_outcome(compared.baseline, read_driver_outcome(env, compared.resumed_sdk, terminal_replay=True))
        with env.sessions._engine.begin() as conn:
            target = conn.execute(select(chat_messages_table).where(
                chat_messages_table.c.session_id == str(compared.case.session_id),
                chat_messages_table.c.role == "tool", chat_messages_table.c.composition_state_id.is_not(None),
                chat_messages_table.c.composition_state_id != str(compared.case.base_state_id),
            ).order_by(chat_messages_table.c.sequence_no)).first()
            assert target is not None, "The positive fixture must contain an actual P4-bound tool row"
            changed = conn.execute(update(chat_messages_table).where(chat_messages_table.c.id == target.id).values(
                composition_state_id=str(compared.case.base_state_id),
            ))
            assert changed.rowcount == 1
        with pytest.raises(AssertionError):
            assert_same_driver_outcome(compared.baseline, read_driver_outcome(env, compared.resumed_sdk, terminal_replay=True))
    finally:
        if target is not None:
            with env.sessions._engine.begin() as conn:
                conn.execute(update(chat_messages_table).where(chat_messages_table.c.id == target.id).values(
                    composition_state_id=target.composition_state_id,
                ))
        env.dispose()


@pytest.mark.asyncio
async def test_oracle_keeps_actual_uuid_looking_user_text_exact(operation_store, tmp_path, monkeypatch):
    compared = await compare_driver_cut(operation_store, tmp_path, monkeypatch, "ingress")
    env = fresh_driver_environment(compared.cold_database_url, compared.case)
    user = None
    try:
        with env.sessions._engine.begin() as conn:
            user = conn.execute(select(chat_messages_table).where(
                chat_messages_table.c.id == compared.case.original_user_message_id,
            )).one()
            assert "00000000-0000-4000-8000-000000009999" in user.content
            conn.execute(update(chat_messages_table).where(chat_messages_table.c.id == user.id).values(
                content=user.content.replace("00000000-0000-4000-8000-000000009999", "00000000-0000-4000-8000-000000008888"),
            ))
        with pytest.raises(AssertionError):
            read_driver_outcome(env, compared.resumed_sdk, terminal_replay=True)
    finally:
        if user is not None:
            with env.sessions._engine.begin() as conn:
                conn.execute(update(chat_messages_table).where(chat_messages_table.c.id == user.id).values(content=user.content))
        env.dispose()
```

- [ ] Create `tests/unit/web/composer/test_continuation_provider_configuration.py`
  with the complete real-custody test below. `make_custody` is the fully
  defined storage C4b.2 fixture adapter; this adds physical endpoint/model/
  custom-provider controls to its actual expected-input binding. Only the
  SDK is replaced. `api_base` belongs to the safe input digest; live
  `api_key`, application headers and timeouts do not.

```python
from __future__ import annotations

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_gateway import _litellm_acompletion
from elspeth.web.sessions.models import quota_provider_attempts_table
from tests.unit.web.composer.test_continuation_provider_accounting import make_custody
from tests.unit.web.composer.test_prompt_guard_admission import _checks
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", (
    {"api_base": "https://changed.invalid/v1"},
    {"model": "openai/gpt-4o-mini"},
    {"custom_llm_provider": "openai"},
))
async def test_changed_physical_configuration_refuses_before_guard_quota_or_sdk(operation_store, monkeypatch, changed):
    engine, repository, _authority, _service, _sid = operation_store
    running, coordinator, _binding, custody = make_custody(operation_store)
    physical = []

    async def sdk(**kwargs):
        physical.append(kwargs)
        raise AssertionError("Changed actual provider configuration reached the SDK")

    kwargs = {
        "model": "test-model", "messages": [{"role": "user", "content": "Read the actual plugin contract"}],
        **changed,
    }
    monkeypatch.setattr("litellm.acompletion", sdk)
    try:
        with prompt_guard_user_scope("Read the actual plugin contract"):
            with pytest.raises(AuditIntegrityError, match="differs from its prepared effect"):
                await _litellm_acompletion(
                    prompt_guard_mount=None, prompt_guard_mode="debug-when-unconfigured",
                    prompt_guard_role="composer", provider_custody=custody, **kwargs,
                )
        assert physical == [] and _checks(engine) == []
        with engine.connect() as conn:
            assert conn.execute(select(quota_provider_attempts_table).where(
                quota_provider_attempts_table.c.operation_id == running.session_operation_context.fence.operation_id,
            )).all() == []
        coordinator.assert_completed()
    finally:
        repository.release(running.session_operation_context)
```

## D8: Establish behavioral RED, GREEN and the normal integration gates

Before D8, append these complete current-resolver controls to the same
integration test module. The wrapper performs one deliberately corrupt
pytest-owned SQL update, calls the **actual** resolver, preserves its actual
exception, and restores the original value. None of these corrupted values
can reach a subsequent semantic SDK call. Changing `ledger.model` tests its
physical model identity while retaining the database's actor/session/source
FKs; no FK/trigger/clock is disabled to manufacture a cross-principal row.

```python
import json
import litellm

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.advisor_output import parse_continuation_advisor_decision
from elspeth.web.composer.continuation_actions import ContinuationAdvisorContext
from elspeth.web.composer.continuation_progress import ContinuationProtocolError, require_bound_continuation_decision
from elspeth.web.sessions.models import (
    composer_continuation_chunks_table as chunks,
    composer_continuation_effects_table as effects,
    quota_provider_attempts_table as attempts,
    token_usage_ledger_table as ledger,
)
from tests.helpers.continuation_driver import (
    clone_admitted_database, ordinary_driver_script, run_driver_attempt, seed_driver_case,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ("p4_graph", "read_chunk", "provider_ledger_model"))
async def test_actual_resolver_refuses_corrupt_owner_before_next_sdk(
    operation_store, tmp_path, monkeypatch, corruption,
):
    case = await seed_driver_case(operation_store, tmp_path / "data")
    env = fresh_driver_environment(clone_admitted_database(case, tmp_path / "corrupt.db"), case)
    sdk = ordinary_driver_script(case)
    actual_resolve = env.authority.resolve_continuation_frontier_evidence
    observed = []
    sdk_counts = []
    injected = False

    def resolve(running, *, expected_checkpoint_id, expected_cursor):
        nonlocal injected
        assert not injected, "This fault case must refuse its first actual END resolver"
        injected = True
        sdk_counts.append(len(sdk.invocations))
        table = None
        row_key = None
        original = None
        try:
            with env.sessions._engine.begin() as conn:
                if corruption == "p4_graph":
                    row = conn.execute(select(effects).where(
                        effects.c.operation_id == case.operation_id, effects.c.owner == "session_sql",
                        effects.c.disposition == "committed", effects.c.graph_sha256.is_not(None),
                    ).order_by(effects.c.effect_key)).first()
                    assert row is not None and row.graph_sha256 != "0" * 64
                    table, row_key, original = effects, row.effect_key, {"graph_sha256": row.graph_sha256}
                    conn.execute(update(effects).where(effects.c.operation_id == case.operation_id,
                        effects.c.effect_key == row.effect_key).values(graph_sha256="0" * 64))
                elif corruption == "read_chunk":
                    effect = conn.execute(select(effects).where(
                        effects.c.operation_id == case.operation_id, effects.c.owner == "read",
                        effects.c.disposition == "committed",
                    ).order_by(effects.c.effect_key)).first()
                    assert effect is not None
                    row = conn.execute(select(chunks).where(chunks.c.operation_id == case.operation_id,
                        chunks.c.chunk_key == effect.result_chunk_key)).one()
                    assert row.payload and row.payload[:1] == b"{"
                    table, row_key, original = chunks, row.chunk_key, {"payload": row.payload}
                    conn.execute(update(chunks).where(chunks.c.operation_id == case.operation_id,
                        chunks.c.chunk_key == row.chunk_key).values(payload=b"[" + row.payload[1:]))
                else:
                    effect = conn.execute(select(effects).where(
                        effects.c.operation_id == case.operation_id, effects.c.owner == "provider",
                        effects.c.disposition == "committed", effects.c.provider_attempt_id.is_not(None),
                    ).order_by(effects.c.effect_key)).first()
                    assert effect is not None
                    attempt = conn.execute(select(attempts).where(attempts.c.attempt_id == effect.provider_attempt_id)).one()
                    row = conn.execute(select(ledger).where(ledger.c.entry_id == attempt.ledger_entry_id)).one()
                    table, row_key, original = ledger, row.entry_id, {"model": row.model}
                    conn.execute(update(ledger).where(ledger.c.entry_id == row.entry_id).values(model="changed-physical-model"))
            try:
                return actual_resolve(running, expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
            except BaseException as error:
                observed.append(error)
                raise
        finally:
            if table is not None:
                assert original is not None
                with env.sessions._engine.begin() as conn:
                    if table is effects:
                        conn.execute(update(effects).where(effects.c.operation_id == case.operation_id,
                            effects.c.effect_key == row_key).values(**original))
                    elif table is chunks:
                        conn.execute(update(chunks).where(chunks.c.operation_id == case.operation_id,
                            chunks.c.chunk_key == row_key).values(**original))
                    else:
                        conn.execute(update(ledger).where(ledger.c.entry_id == row_key).values(**original))

    monkeypatch.setattr(env.authority, "resolve_continuation_frontier_evidence", resolve)
    monkeypatch.setattr(litellm, "acompletion", sdk.complete)
    try:
        await run_driver_attempt(env, sdk, monkeypatch, expect_suspension=True)
        assert injected and len(observed) == 1 and isinstance(observed[0], AuditIntegrityError)
        assert sdk_counts == [len(sdk.invocations)]
        assert not any(item.role == "semantic" for item in sdk.invocations)
    finally:
        env.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", (
    ("source_kind", "provider"), ("content_hash", "0" * 64), ("coverage_binding", "forged-coverage"),
))
async def test_model_cannot_reclassify_or_rebind_actual_resolver_evidence(
    operation_store, tmp_path, monkeypatch, field, value,
):
    compared = await compare_driver_cut(operation_store, tmp_path, monkeypatch, "end_response")
    semantic = next(item for item in compared.resumed_sdk.invocations if item.role == "semantic")
    messages = json.loads(semantic.wire_json)["messages"]
    contexts = []
    for message in messages:
        if message["role"] == "user" and type(message["content"]) is str:
            try:
                data = json.loads(message["content"])
            except json.JSONDecodeError:
                continue
            if type(data) is dict and "question_id" in data:
                contexts.append(ContinuationAdvisorContext.model_validate_json(message["content"]))
    assert len(contexts) == 1
    context = contexts[0]
    env = fresh_driver_environment(compared.cold_database_url, compared.case)
    try:
        with env.sessions._engine.connect() as conn:
            rows = conn.execute(select(chat_messages_table).where(
                chat_messages_table.c.session_id == str(compared.case.session_id),
                chat_messages_table.c.role == "audit",
            )).all()
        actual_calls = [entry["call"] for row in rows for entry in (row.tool_calls or ())
                        if entry["_kind"] == "llm_call_audit" and entry["call"]["provider_request_id"] == "sdk-semantic-0"]
        assert len(actual_calls) == 1
        actual_id = actual_calls[0]["call_id"]
        assert semantic.response_content is not None
        decision = parse_continuation_advisor_decision(semantic.response_content, actual_advisor_call_id=actual_id)
        assert decision is not None
        retained = {item.receipt_id: item for item in context.frontier.evidence}
        arguments = dict(
            actual_advisor_call_id=actual_id, frontier=context.frontier, retained_evidence=retained,
            input_hash=context.input_hash, policy_hash=context.policy_hash,
            tool_manifest_hash=context.tool_manifest_hash, permitted_read_tools=frozenset(),
            complete_required_coverage=True,
        )
        require_bound_continuation_decision(decision, **arguments)
        selected = next(item for item in decision.evidence if item.source_kind == "graph")
        changed = selected.model_copy(update={field: value})
        assert changed != selected
        forged = decision.model_copy(update={"evidence": tuple(changed if item is selected else item for item in decision.evidence)})
        with pytest.raises(ContinuationProtocolError, match="unretained evidence"):
            require_bound_continuation_decision(forged, **arguments)
    finally:
        env.dispose()
```

The untouched D5 cases are the positive control for the same real resolver.
The provider ledger mutation tests model identity, and the read mutation
preserves byte length; neither relies on a schema syntax error to avoid the
intended owner check.

These are future execution commands after main P0 and complete driver/batch/
native-materializer prerequisites. They have **not** been executed while
authoring. Use the ordinary main execution setup: both worktree source roots
on `PYTHONPATH`, the checked interpreter, `COMPOSER_LOG_DIR` and a
project-local `TMPDIR`. A module/fixture/collection failure is setup failure,
not behavioral RED. Do not change expected results to accept a missing cut.

- [ ] Bootstrap the named production/test modules, then establish the native
  preservation RED with this literal controlled edit in
  `composer/continuation_codec.py::encode_continuation_response`:

```diff
-            thinking_blocks=owned_json(metadata.thinking_blocks),
+            thinking_blocks=None,
```

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest tests/integration/web/composer/test_continuation_driver_equivalence.py::test_native_blocks_preserve_full_tail_signatures_order_and_exact_next_wire -n 0 -v > "$COMPOSER_LOG_DIR/red-driver-native.log" 2>&1
COMPOSER_RC=$?
cat "$COMPOSER_LOG_DIR/red-driver-native.log"
test "$COMPOSER_RC" -eq 1
```

- [ ] Restore the exact native assignment. Establish the physical binding RED
  by changing only C5.8's comparison as shown below, leaving both input-hash
  calculations real. This deliberate invalid condition must fail all three
  pre-dispatch refusal controls before being restored:

```diff
-    if continuation_provider_input_hash(kwargs) != binding.input_sha256:
+    if continuation_provider_input_hash(kwargs) != continuation_provider_input_hash(kwargs):
         raise AuditIntegrityError("actual continuation provider input differs from its prepared effect")
```

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest tests/unit/web/composer/test_continuation_provider_configuration.py -n 0 -v > "$COMPOSER_LOG_DIR/red-driver-input.log" 2>&1
COMPOSER_RC=$?
cat "$COMPOSER_LOG_DIR/red-driver-input.log"
test "$COMPOSER_RC" -eq 1
```

- [ ] Restore the binding comparison. Run the complete new packet through
  normal pytest, recording native exit 0 and the complete log. The process
  death test terminates only its own spawned fixture process.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest tests/integration/web/composer/test_continuation_driver_equivalence.py tests/integration/web/composer/test_continuation_driver_process_death.py tests/unit/web/composer/test_continuation_provider_configuration.py -n 0 -v > "$COMPOSER_LOG_DIR/green-real-driver.log" 2>&1
COMPOSER_RC=$?
cat "$COMPOSER_LOG_DIR/green-real-driver.log"
test "$COMPOSER_RC" -eq 0
```

- [ ] Run the actual owner integration/negative-control packets as part of
  the same checkpoint. The C3 proposal, interpretation and versioned-read
  tests exercise their complete writers and retained results; they must not
  be counted as full-driver proposal/interpretation coverage unless the
  actual end-to-end scenario is also supplied.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest tests/unit/web/sessions/test_continuation_cold_restore.py tests/unit/web/sessions/test_composer_continuation_worker_adoption.py tests/unit/web/composer/test_continuation_provider_accounting.py tests/unit/web/composer/test_continuation_evidence.py tests/unit/web/composer/test_continuation_prompt_producers.py tests/unit/web/composer/test_continuation_caches.py tests/unit/web/sessions/test_run_composer_turn.py tests/unit/web/composer/test_provider_quota_end_to_end.py -n 0 -v > "$COMPOSER_LOG_DIR/green-real-driver-owners.log" 2>&1
COMPOSER_RC=$?
cat "$COMPOSER_LOG_DIR/green-real-driver-owners.log"
test "$COMPOSER_RC" -eq 0
```

- [ ] Run whole-tree gates affected by these production/test definitions and
  focused quality. Do not add dynamic-attribute suppressions, fixture
  masquerade sites or invented contract counts.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest tests/unit/web/test_sessions_composer_attribute_contracts.py tests/unit/elspeth_lints/test_masquerade_gate.py tests/unit/scripts/test_check_contracts.py -n 0 -v > "$COMPOSER_LOG_DIR/green-driver-gates.log" 2>&1
COMPOSER_RC=$?
cat "$COMPOSER_LOG_DIR/green-driver-gates.log"
test "$COMPOSER_RC" -eq 0
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m ruff check tests/helpers/continuation_driver.py tests/integration/web/composer/test_continuation_driver_equivalence.py tests/integration/web/composer/test_continuation_driver_process_death.py tests/unit/web/composer/test_continuation_provider_configuration.py > "$COMPOSER_LOG_DIR/ruff-driver-tests.log" 2>&1
COMPOSER_RC=$?
cat "$COMPOSER_LOG_DIR/ruff-driver-tests.log"
test "$COMPOSER_RC" -eq 0
```

- [ ] Complete main Q's ordinary full runtime and serial PostgreSQL gates
  once on the integrated production tree with actual host capacity. These
  focused tests do not replace either gate. Run the existing required
  contract producer if its census changes; do not hand-edit counts or
  operator signatures.
- [ ] Commit only the four named test/helper paths and any reviewed source
  fixes after the packet and affected gates are GREEN. Do not commit a test
  module that imports an unfinished phase driver. Use normal hooks:

```bash
cd "$COMPOSER_WORKTREE" && git status --short
cd "$COMPOSER_WORKTREE" && git diff --check
cd "$COMPOSER_WORKTREE" && git add tests/helpers/continuation_driver.py tests/integration/web/composer/test_continuation_driver_equivalence.py tests/integration/web/composer/test_continuation_driver_process_death.py tests/unit/web/composer/test_continuation_provider_configuration.py
cd "$COMPOSER_WORKTREE" && git diff --cached --stat
cd "$COMPOSER_WORKTREE" && scripts/branch-safety-check.sh --intent commit
cd "$COMPOSER_WORKTREE" && git commit -m "test(composer): prove cold driver custody and outcome equivalence"
```

## Explicit material boundaries for assembly

This packet supplies complete local SDK scripts, actual-store/fresh-owner
fixtures, cut selectors, cooperative and real spawned-child fault tests,
relationship/accounting/evidence readers, oracle controls and commands. It
does not claim those proposed implementation APIs exist at b003 or that any
future test passed.

Before the assembled plan can claim complete full-driver coverage, its
frames/core owners must finish and bind:

1. The actual `advance_continuation`/C4 runner/batch bodies at every named
   phase, including committed `ComposeFrame.bootstrap` and required
   `ComposeFrame.iteration`, all actual chunks and the current operation
   authority. A missing named cut fails D5 rather than becoming coverage.
2. Main C6.5's now-literal SQL `resolve_continuation_frontier_evidence`
   implementation and `ContinuationEvidenceRef.source_kind` contract must be
   implemented before the tests. D3 observes that exact real port; D4 checks
   its returned DTOs against the final frontier. This is a defined execution
   dependency, not an unknown source-hash resolver.
3. Implement producers T6–T10 and attachment-history A2a's complete current
   native/attachment replay and fresh-use bridge. Their literal source bodies,
   actual owner ports, real SQL/cold tests and PostgreSQL reuse are now supplied.
   Exact JSON blocks and historical guard evidence alone remain insufficient.
4. Run the [frames F35 proposal and interpretation packet](2026-10-21-composer-continuation-frames.md#f35--resume-real-unapplied-proposals-and-existing-graph-review-cards)
   after D1–D5. It contains the finite explicit-approval proposal and two
   interpretation scenarios, actual writer/finalization cuts, exact current
   graph/card binding checks, and cold full-driver tests. The metadata turn
   completes its actual P4 before model review. Backend prompt-template cards
   use the real finalization owner. C3's separate owner tests remain required.
5. Every retained plugin-schema producer recipe must recover its original
   producer input inventory from actual receipt/chunk-bound source
   descriptors in bootstrap/iteration. An empty cold `plugin_schema_inputs`
   tuple cannot reconstruct a current producer for an existing nonempty
   recipe. Advisor request messages stay local to the Advisor body; F13
   reconstructs them from the exact active invocation source and current
   owned builders. The tests resume after actual primary schema disclosure
   and active Advisor input retention, without inventing a shared Advisor
   message recipe.
6. Run the [frames F34 prescribed-read packet](2026-10-21-composer-continuation-frames.md#f34--prove-a-prescribed-read-survives-its-actual-sql-result-cut)
   after D1–D5. It supplies the complete finite read SDK, the actual retained-read
   SQL cut, exact baseline/cold script selection, and its separate integration
   test. Preserve D3's real executor/SQL observation and its safe-only public
   advance contract; a completed read creates no synthetic ToolInvocation.

F34/F35 are mandatory source packets in this plan, not evidence that their
future tests ran. The remaining runner, physical-outcome and producer owners
must be integrated before this driver packet is executed. Runtime completion
requires the actual native test results and ordinary combined-tree gates.

## D9: Deliver an external cancellation before granting prepared business authority

**Modify:** `tests/integration/web/composer/test_continuation_driver_equivalence.py`;
use the D3 `business_prepared` cut and F35's complete review SDK/helper additions.
**Source under test:** B5 `ContinuationBatchExecution.prepare_owner` in
`src/elspeth/web/composer/tool_batch.py`.

- [ ] Add the complete test below after implementing B5, D3 and F35. The cut
  holds the actual registered preparation SQL future after its transaction
  committed and before finish-once returns it. It never substitutes a SQL
  result, business writer, provider custody, worker, or recovery receipt.
  The ordinary real SDK script still traverses validation and preparation.

```python
import litellm
import pytest
from sqlalchemy import select

from elspeth.web.composer import tool_batch as tool_batch_module
from elspeth.web.composer.continuation import ToolBatchFrame
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot
from elspeth.web.composer.continuation_tool_batch import decode_batch_chunk
from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
from elspeth.web.sessions.models import (
    composer_continuation_effects_table, composition_proposals_table,
    interpretation_events_table,
)
from tests.helpers.continuation_driver import (
    REVIEW_SECRET, DriverCutObservation, failure_objects, fresh_driver_environment,
    review_handoff_driver_script, run_driver_attempt, seed_driver_case,
    select_driver_state,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ("proposal", "interpretation_tool"))
async def test_external_cancel_after_actual_prepare_returns_before_business_entry(
    operation_store, tmp_path, monkeypatch, scenario,
):
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "continuation-review-fixture-fingerprint")
    monkeypatch.setenv(REVIEW_SECRET, "continuation-review-fixture-provider-material")
    case = await seed_driver_case(operation_store, tmp_path / "data",
        trust_mode="explicit_approve" if scenario == "proposal" else "auto_commit",
        review_fixture="prompt" if scenario == "interpretation_tool" else "none")
    env = fresh_driver_environment(str(operation_store[0].url), case)
    cut = DriverCutObservation("business_prepared")
    sdk = review_handoff_driver_script(case, scenario)
    entries = []
    originals = []
    actual_proofs = []
    actual_finish = tool_batch_module.run_required_sql_finish_once
    actual_finished = ComposerAsyncWorker._job_finished
    actual_proposal = env.sessions.create_composition_proposal
    actual_interpretation = env.sessions.create_pending_interpretation_event

    def business_rows():
        with env.sessions._engine.connect() as conn:
            return tuple(tuple(conn.execute(select(table).where(
                table.c.session_id == str(case.session_id)).order_by(table.c.id)).all())
                for table in (composition_proposals_table, interpretation_events_table))

    async def finish(*args, **kwargs):
        actual = await actual_finish(*args, **kwargs)
        if kwargs.get("owner") == "session_sql":
            originals.extend(actual.deferred_cancellations)
        return actual

    async def proposal(*args, **kwargs):
        entries.append("proposal")
        return await actual_proposal(*args, **kwargs)

    async def interpretation(*args, **kwargs):
        entries.append("interpretation")
        return await actual_interpretation(*args, **kwargs)

    def finished(worker, key, task):
        if key == (case.session_id, case.operation_id):
            actual_proofs.append(worker._continuation_outcomes[key])
        return actual_finished(worker, key, task)

    before = business_rows()
    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(litellm, "acompletion", sdk.complete)
            patcher.setattr(tool_batch_module, "run_required_sql_finish_once", finish)
            patcher.setattr(env.sessions, "create_composition_proposal", proposal)
            patcher.setattr(env.sessions, "create_pending_interpretation_event", interpretation)
            patcher.setattr(ComposerAsyncWorker, "_job_finished", finished)
            await run_driver_attempt(env, sdk, patcher, cut=cut)
        assert cut.selected is not None and originals
        assert len(actual_proofs) == 1 and actual_proofs[0].original is not None
        assert all(any(item is original for item in failure_objects(actual_proofs[0].original))
                   for original in originals)
        env.authority.verify_recovery(actual_proofs[0].receipt)
        assert entries == [], "Prepared cancellation must propagate before entering either actual business writer"
        assert business_rows() == before
        selected = select_driver_state(env)
        root = decode_continuation_snapshot(selected.payload_json)
        prepared_root = decode_continuation_snapshot(cut.selected.payload_json)
        assert selected.checkpoint_id == actual_proofs[0].receipt.checkpoint_id
        assert selected.checkpoint_id != cut.selected.checkpoint_id
        assert selected.cursor == cut.selected.cursor + 1 == actual_proofs[0].receipt.cursor
        assert root == prepared_root.model_copy(update={"cursor": prepared_root.cursor + 1})
        assert selected.chunks == cut.selected.chunks
        top = root.frames[-1]
        assert type(top) is ToolBatchFrame
        batch = decode_batch_chunk(PrivateChunk(top.accumulator, dict(selected.chunks)[top.accumulator.digest]))
        call = batch.current_call
        assert call is not None and call.effect_key is not None and call.phase == "prepared"
        assert not root.pending_owner_results and call.owner_result_json is None
        with env.sessions._engine.connect() as conn:
            effect = conn.execute(select(composer_continuation_effects_table).where(
                composer_continuation_effects_table.c.session_id == str(case.session_id),
                composer_continuation_effects_table.c.operation_id == case.operation_id,
                composer_continuation_effects_table.c.effect_key == call.effect_key)).one()
        assert effect.owner == "session_sql" and effect.disposition == "prepared"
        assert effect.result_chunk_key is None and effect.receipt_key is None
        assert sdk.invocations == list(cut.sdk_prefix)
    finally:
        env.dispose()
```

- [ ] Behavioral RED: remove only B5's new `self.raise_deferred()` after the
  current SQL boundary read and before its final Stop/return branch, retaining
  every transaction guard.
  Run the exact command below and require a native nonzero exit at the
  `entries == []` assertion. A later writer guard, setup exception, or missing
  cut does not satisfy this control. Restore the cancellation delivery and
  require native exit 0 for both actual proposal and interpretation paths:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest tests/integration/web/composer/test_continuation_driver_equivalence.py::test_external_cancel_after_actual_prepare_returns_before_business_entry -n 0 -v > "$COMPOSER_LOG_DIR/prepared-business-cancel.log" 2>&1
COMPOSER_RC=$?
cat "$COMPOSER_LOG_DIR/prepared-business-cancel.log"
test "$COMPOSER_RC" -eq 0
```

`run_driver_attempt` independently verifies the real cohort, released lease,
joined renewal/close tasks, recovery readback, original external marker and
process-lock release. This test additionally binds the very same deferred
exception objects to the worker's retained proof before its normal callback
retires custody. The unchanged prepared row is recoverable; no business result
or ToolInvocation is invented to make cancellation appear complete.
