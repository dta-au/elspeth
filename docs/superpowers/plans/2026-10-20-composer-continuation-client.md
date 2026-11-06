# Composer Continuation Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Observe and stop one durable Composer request across safe segment rollover, with a closed versioned protocol and quiet activity notices.

**Architecture:** Preserve exact v1 request/hash/observation behavior and add explicit v2 negotiation over the same authenticated session routes. The SQL owner admits and resumes segments, while one browser custody record retains the immutable original operation, requested protocol and Stop intent. Selected existing .7 fixes remain represented semantically in the combined source.

**Tech Stack:** Python 3.12+, Pydantic, FastAPI/Starlette, SQLAlchemy, TypeScript, React, Zustand, Vitest and pytest.

---

This companion implements C7, C8 and S7 of [the main plan](2026-10-20-composer-continuation.md). The [storage companion](2026-10-20-composer-continuation-storage.md) owns C2/C3/C1b/C4a/C4b/C5, including actual SQL contention and provider accounting. Main C1a/C6 owns the complete continuation codec and driver. Execute this companion only after those dependencies reach their stated exits.

The main plan records the user's current implementation authority, actual
combined source and qualification boundaries. Local `.9` qualification is
complete and its isolated `.10` integration exists. Use ordinary repository
workflow and actual resource measurements; no shared test-capacity reservation
or bespoke controller is added here.

Immutable extraction anchor is `b003e0cd9c6c6a7b0968635a9c03f71a83bc2e73`, tree `6a34e912debc469e9ce7e2048d433c08d5e606a9`, parent `ce4103539fe97f07fc60fb0cd096256c207c8bc5`. Its production delta from the original client reference ce410 changes Advisor request-failure classification, explicit unconfigured guard size policy and tool-batch request-rejection evidence. Preserve current `client.ts` validation-check admission, storage-quota exact quantities and application-specific 429 refusal handling. The current target additionally includes the main plan's selected later repairs; old line numbers do not authorize overwriting them.

The later ordinary frontend checkpoint is
`52543bcc195419561b32412c79f4004e3fa8af62`, parent `c299f2e7`, tree
`e6cb88e8d1c11de4ea2ae653b5971201f6472c33`. Its exact two-path delta removes
request-text guessing from `ComposingIndicator.tsx` and updates its tests.
When P0's owner-directed integration includes that checkpoint, apply C8's
additional props/notice to its neutral `waitingWorkingView` baseline and keep
those existing neutral-waiting regressions. None of the C8 steps restores
`heuristicWorkingView`, request keyword matching or estimated progress.
The neutral checkpoint is already carried in the main plan's committed
integration history. Its focused frontend result remains separate from
qualification of the patched dependency runtime and these future C8 changes.

All task commands are future commands. P0 supplies the admitted `COMPOSER_WORKTREE` and `COMPOSER_PYTHON`; do not create/install/symlink an environment here. In that authorized checkout, create one ordinary project-local log directory:

```bash
cd "$COMPOSER_WORKTREE" && COMPOSER_CLIENT_LOG_DIR="$PWD/.claude/lanes/composer-continuation-client-$(date -u +%Y%m%dT%H%M%SZ)" && mkdir -p "$COMPOSER_CLIENT_LOG_DIR"
```

Read each test process's full log and native exit. A bootstrap import failure only proves the named new module is absent; obtain the behavioral RED after the minimal module exists and before implementing its assertion. A failed prerequisite/import/collection is not the requested behavioral RED. Commands below do not pipe suites. Before each commit, review the staged path list and preserve unrelated dirty or staged state. Run focused Ruff and relevant whole-tree gates before ordinary hooks; never bypass hooks or edit signatures.

## Agreed core boundary

The core tasks must implement these exact synchronous facets on
`SessionServiceProtocol`, backed by one session transaction per call. The
authenticated principal/provider binding and session ownership are checked in
the same transaction that reads/changes the parent and current segment. `None`
means no readable owned parent. The read returns a complete public projection;
it does not return checkpoint, provider, tool or private request payloads.

```python
def get_composer_operation_v2_snapshot(
    self,
    *,
    session_id: UUID,
    operation_id: str,
    actor_user_id: str,
    auth_provider_type: str,
) -> ComposerOperationV2Snapshot | None: ...

def request_composer_operation_v2_cancel(
    self,
    *,
    session_id: UUID,
    operation_id: str,
    actor_user_id: str,
    auth_provider_type: str,
) -> tuple[ComposerOperationV2Snapshot, datetime] | None: ...
```

The second cancellation tuple item is the database cancellation transaction
time. A terminal parent remains terminal; a completed parent cannot acquire
`cancel_requested=True`. A live cancellation revokes authority transactionally
before any local provider cancellation notification. The current v1
`ComposerAsyncOperationAuthority.request_cancel` path remains available for v1.

`ComposerOperationRecord` must expose `lifecycle_version: Literal[1, 2]` from
the core task; the v1 `get_with_database_now` read remains available for
authenticated lifecycle dispatch. It must accept a v2 row with null parent
deadline without interpreting that as malformed v1 state.

## File map

| File | Responsibility |
| --- | --- |
| `src/elspeth/web/sessions/composer_continuation_dtos.py` (new) | Closed v2 public snapshot, segment, SSE DTOs and exact v1 cancellation projection. |
| `src/elspeth/web/sessions/composer_operation_versions.py` (new) | Header negotiation, lifecycle hash domain separation, read compatibility. |
| `src/elspeth/web/sessions/routes/composer/operations.py` | Dispatch only after authentication/ownership; public projection; Stop. |
| `src/elspeth/web/sessions/routes/messages.py`, `routes/composer/compose.py` | Expose the v1/v2 admission response union and response version header. |
| `src/elspeth/web/frontend/src/types/composerOperations.ts` | Explicit v2 types and immutable custody protocol version. |
| `src/elspeth/web/frontend/src/api/composerOperationV2Decoder.ts` (new) | Closed v2 decoder, reusing existing strict result/error admissions. |
| `src/elspeth/web/frontend/src/api/composerContinuationState.ts` (new) | Durable revision ordering, fixed running deadline and successor notice decisions. |
| `src/elspeth/web/frontend/src/api/client.ts` | Immutable request-version header, selected response-version decoding. |
| `src/elspeth/web/frontend/src/stores/composerOperationCustody.ts` | Persist requested version; restore historical absent version as 1. |
| `src/elspeth/web/frontend/src/api/composerOperationObserver.ts` | One logical observer; segment expiry reconnects rather than detaches. |
| `src/elspeth/web/app.py`, `src/elspeth/web/frontend/src/types/index.ts`, `src/elspeth/web/frontend/src/App.tsx`, `src/elspeth/web/frontend/src/config/composer.ts` | New-admission capability only. |
| `src/elspeth/web/frontend/src/stores/sessionStore.ts`, `hooks/useComposer.ts`, `components/chat/ChatPanel.tsx`, `components/chat/ComposingIndicator.tsx` | Current operation phase and ephemeral successor notice. |
| The eleven selected shared-auth/source no-op paths listed in S7.1 | Preserve the changes already represented by immutable ce410; no staged SMK10 import. |

### Task C7.0: Disabled-by-default admission flag and existing dependency injection

**Modify:** `src/elspeth/web/config.py` at the existing async Composer settings;
`src/elspeth/web/app.py` at the `ComposerAsyncOperationAuthority` constructor.
**Test:** `tests/unit/web/sessions/test_composer_operation_settings.py`.

- [ ] Add this exact test to the existing settings test module, which already
  imports `_settings` from `tests.unit.web.test_config`.

```python
def test_continuation_admission_is_disabled_by_default() -> None:
    assert _settings().composer_continuation_enabled is False
    assert _settings(composer_continuation_enabled=False).composer_continuation_enabled is False
```

- [ ] Run the named test before adding the field; expect native 1 from missing
  owned field, not an unrelated import or configuration failure.

```bash
cd "$COMPOSER_WORKTREE" && {
  PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" "$COMPOSER_PYTHON" -m pytest tests/unit/web/sessions/test_composer_operation_settings.py::test_continuation_admission_is_disabled_by_default -n 0 -q > "$COMPOSER_CLIENT_LOG_DIR/c7-flag-red.log" 2>&1
  client_check_status=$?
  cat "$COMPOSER_CLIENT_LOG_DIR/c7-flag-red.log"
  printf 'native_exit=%s\n' "$client_check_status"
  test "$client_check_status" -eq 1
}
```

- [ ] Add this setting directly beside `composer_async_max_queued_operations`.

```python
composer_continuation_enabled: bool = False
```

The existing `ELSPETH_WEB__` settings namespace makes its environment name
`ELSPETH_WEB__COMPOSER_CONTINUATION_ENABLED`. Only Q may enable new v2
admissions. Disabling it later keeps admitted v2 reads, Stop, idempotent
replays and segment draining available; the stored lifecycle still owns them.

- [ ] Import `ContinuationAdmissionPolicy` from
  `elspeth.web.composer.continuation_clock` in `app.py`. Replace only the
  existing authority constructor with this complete call; do not change the
  worker, ownership identity, services or queue limits.

```python
composer_async_authority = ComposerAsyncOperationAuthority(
    session_engine,
    owner_instance_id=session_service.session_operation_owner_instance_id,
    claim_lease_seconds=settings.composer_async_claim_lease_seconds,
    continuation_policy=(
        ContinuationAdmissionPolicy.from_settings(settings)
        if settings.composer_continuation_enabled else None
    ),
)
```

The storage companion defines that nominal policy, validates all configured
role bounds/reserve when enabled, and retains the complete owned policy in the
initial snapshot. It builds the seed inside the authenticated admission SQL
transaction after exact-key replay and feature-gate checks. The HTTP body
contains no seed or timing policy, and the route constructs none.

- [ ] Run the settings module after adding the field and DI; require native 0.
  Stage only these paths after focused checks and branch preflight, then use
  ordinary `git commit -m "feat(composer): gate new continuation admission by configuration"`.

```bash
cd "$COMPOSER_WORKTREE" && {
  PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" "$COMPOSER_PYTHON" -m pytest tests/unit/web/sessions/test_composer_operation_settings.py -n 0 -q > "$COMPOSER_CLIENT_LOG_DIR/c7-flag-green.log" 2>&1
  client_check_status=$?
  cat "$COMPOSER_CLIENT_LOG_DIR/c7-flag-green.log"
  printf 'native_exit=%s\n' "$client_check_status"
  test "$client_check_status" -eq 0
}
cd "$COMPOSER_WORKTREE" && git status --short
cd "$COMPOSER_WORKTREE" && git add -- src/elspeth/web/config.py src/elspeth/web/app.py tests/unit/web/sessions/test_composer_operation_settings.py
cd "$COMPOSER_WORKTREE" && git diff --cached --name-only
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" scripts/branch-safety-check.sh --intent commit
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" git commit -m "feat(composer): gate new continuation admission by configuration"
```

### Task C7.1: Closed v2 public DTOs and exact old-client cancellation ACK

**Create:** `src/elspeth/web/sessions/composer_continuation_dtos.py`.
**Create:** `tests/unit/web/sessions/test_composer_continuation_dtos.py`.

- [ ] Write the following complete test file before creating the module.

```python
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from elspeth.web.sessions.composer_continuation_dtos import (
    ComposerOperationSegmentV2,
    ComposerOperationV2Snapshot,
    legacy_cancel_projection,
)


def test_live_snapshot_is_closed_and_old_cancel_uses_v1_field_names() -> None:
    now = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
    segment = ComposerOperationSegmentV2(
        sequence=2, generation=3, state="running", observed_at=now,
        deadline_at=now + timedelta(seconds=30), remaining_ms=30_000,
        rollover_pending=False,
    )
    snapshot = ComposerOperationV2Snapshot(
        operation_id="22222222-2222-4222-8222-222222222222",
        kind="compose_message", logical_status="running", phase="executing",
        status_revision=8, poll_after_ms=1000, cancel_requested=True,
        segment=segment, result=None, error=None,
    )
    encoded = snapshot.model_dump(mode="json")
    assert set(encoded) == {
        "protocol_version", "operation_id", "kind", "logical_status", "phase",
        "status_revision", "poll_after_ms", "cancel_requested", "segment", "result", "error",
    }
    old = legacy_cancel_projection(snapshot, cancelled_at=now)
    assert old.deadline_at == now
    assert old.deadline_remaining_ms == 0
    assert old.status == "running" and old.cancel_requested
    assert "remaining_ms" not in old.model_dump()
    for changes in [
        {"status_revision": True}, {"private_checkpoint": "marker"},
        {"phase": "settled"}, {"segment": None}, {"logical_status": "cancelled"},
    ]:
        with pytest.raises(ValidationError):
            ComposerOperationV2Snapshot.model_validate({**snapshot.model_dump(), **changes}, strict=True)


@pytest.mark.parametrize("changes", [
    {"sequence": 0}, {"generation": True}, {"observed_at": datetime(2026, 10, 20)},
    {"state": "queued", "deadline_at": datetime(2026, 10, 20, tzinfo=UTC)},
    {"state": "running", "deadline_at": None, "remaining_ms": None},
    {"claim_token": "marker"},
])
def test_segment_rejects_invalid_or_private_fields(changes: dict[str, object]) -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)
    base = dict(sequence=1, generation=1, state="queued", observed_at=now,
                deadline_at=None, remaining_ms=None, rollover_pending=False)
    with pytest.raises(ValidationError):
        ComposerOperationSegmentV2.model_validate({**base, **changes}, strict=True)
```

- [ ] After adding the importable DTO packet below, temporarily change
  `ComposerOperationSegmentV2.model_config` to
  `ConfigDict(extra="allow", frozen=True, strict=True)`. Run the behavioral
  RED below: the private `claim_token` field must make its rejection test fail.
  Restore `extra="forbid"` before GREEN and never commit the mutation.

```bash
cd "$COMPOSER_WORKTREE" && {
  env PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" "$COMPOSER_PYTHON" -m pytest tests/unit/web/sessions/test_composer_continuation_dtos.py -n 0 -q > "$COMPOSER_CLIENT_LOG_DIR/c7-1-red.log" 2>&1
  client_check_status=$?
  cat "$COMPOSER_CLIENT_LOG_DIR/c7-1-red.log"
  printf 'native_exit=%s\n' "$client_check_status"
  test "$client_check_status" -eq 1
}
```

Expected native 1 with the private-field rejection assertion failing under the
stated mutation. An absent module, import error or collection exit 2 is setup
failure and does not satisfy this RED.

- [ ] Create this complete DTO module.

```python
"""Closed public Continuation projections; private state never enters this module."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from elspeth.web.sessions.composer_operations import (
    ComposerOperationError, ComposerOperationKind, ComposerOperationStatus,
    require_canonical_operation_id,
)
from elspeth.web.sessions.schemas import (
    COMPOSER_OPERATION_STREAM_FRAME_MAX_BYTES, ComposerOperationStatusResponse,
    ComposerOperationStreamProgressPayload, MessageWithStateResponse,
)

type ComposerContinuationPhase = Literal[
    "queued", "executing", "continuation_queued", "reconciling", "retry_waiting", "settled"
]


class ComposerOperationSegmentV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    sequence: int = Field(ge=1)
    generation: int = Field(ge=1)
    state: Literal["queued", "running", "interrupted"]
    observed_at: AwareDatetime
    deadline_at: AwareDatetime | None
    remaining_ms: int | None = Field(ge=0)
    rollover_pending: bool

    @model_validator(mode="after")
    def validate_bundle(self) -> ComposerOperationSegmentV2:
        running = self.state == "running"
        if running != (self.deadline_at is not None and self.remaining_ms is not None):
            raise ValueError("Running segment requires its fixed deadline and remaining time")
        if not running and (self.deadline_at is not None or self.remaining_ms is not None):
            raise ValueError("Queued/interrupted segment has no running deadline")
        return self


class ComposerOperationV2Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    protocol_version: Literal[2] = 2
    operation_id: str
    kind: ComposerOperationKind
    logical_status: ComposerOperationStatus
    phase: ComposerContinuationPhase
    status_revision: int = Field(ge=0)
    poll_after_ms: int = Field(ge=100, le=60_000)
    cancel_requested: bool
    segment: ComposerOperationSegmentV2 | None
    result: MessageWithStateResponse | None
    error: ComposerOperationError | None

    @field_validator("operation_id")
    @classmethod
    def canonical_operation(cls, value: str) -> str:
        return require_canonical_operation_id(value)

    @model_validator(mode="after")
    def validate_bundle(self) -> ComposerOperationV2Snapshot:
        if self.logical_status in ("completed", "failed"):
            if self.phase != "settled" or self.segment is not None:
                raise ValueError("Terminal parent requires settled phase and no public segment")
            if self.logical_status == "completed":
                if self.result is None or self.error is not None or self.cancel_requested:
                    raise ValueError("Invalid completed snapshot")
            elif self.error is None or self.result is not None:
                raise ValueError("Invalid failed snapshot")
        else:
            if self.segment is None or self.result is not None or self.error is not None:
                raise ValueError("Live parent requires a segment and no terminal payload")
            if self.logical_status == "queued" and self.phase != "queued":
                raise ValueError("Initial queued parent requires queued phase")
            if self.logical_status == "running" and self.phase not in (
                "executing", "continuation_queued", "reconciling", "retry_waiting"
            ):
                raise ValueError("Invalid active parent phase")
        return self


def legacy_cancel_projection(
    snapshot: ComposerOperationV2Snapshot, *, cancelled_at: datetime
) -> ComposerOperationStatusResponse:
    if cancelled_at.utcoffset() is None:
        raise ValueError("Cancellation transaction time must be timezone-aware")
    return ComposerOperationStatusResponse(
        operation_id=snapshot.operation_id, kind=snapshot.kind,
        status=snapshot.logical_status, poll_after_ms=snapshot.poll_after_ms,
        cancel_requested=snapshot.cancel_requested, deadline_at=cancelled_at,
        deadline_remaining_ms=0, result=snapshot.result, error=snapshot.error,
    )


class _ComposerOperationFrameV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["composer-operation-stream.v2"] = "composer-operation-stream.v2"
    session_id: str
    operation_id: str
    sequence: int = Field(ge=0)
    status_revision: int = Field(ge=0)

    @field_validator("session_id", "operation_id")
    @classmethod
    def canonical_identity(cls, value: str) -> str:
        return require_canonical_operation_id(value)


class ComposerOperationStatusFrameV2(_ComposerOperationFrameV2):
    event: Literal["status"] = "status"
    payload: ComposerOperationV2Snapshot

    @model_validator(mode="after")
    def validate_payload(self) -> ComposerOperationStatusFrameV2:
        if self.payload.logical_status not in ("queued", "running"):
            raise ValueError("Status event requires a live snapshot")
        if self.payload.operation_id != self.operation_id or self.payload.status_revision != self.status_revision:
            raise ValueError("Status event identity/revision mismatch")
        return self


class ComposerOperationTerminalFrameV2(_ComposerOperationFrameV2):
    event: Literal["terminal"] = "terminal"
    payload: ComposerOperationV2Snapshot

    @model_validator(mode="after")
    def validate_payload(self) -> ComposerOperationTerminalFrameV2:
        if self.payload.logical_status not in ("completed", "failed"):
            raise ValueError("Terminal event requires a terminal snapshot")
        if self.payload.operation_id != self.operation_id or self.payload.status_revision != self.status_revision:
            raise ValueError("Terminal event identity/revision mismatch")
        return self


class ComposerOperationProgressPayloadV2(ComposerOperationStreamProgressPayload):
    segment_sequence: int = Field(ge=1)
    segment_generation: int = Field(ge=1)


class ComposerOperationProgressFrameV2(_ComposerOperationFrameV2):
    event: Literal["progress"] = "progress"
    payload: ComposerOperationProgressPayloadV2


class ComposerOperationHeartbeatFrameV2(_ComposerOperationFrameV2):
    event: Literal["heartbeat"] = "heartbeat"


type ComposerOperationFrameV2 = Annotated[
    ComposerOperationStatusFrameV2 | ComposerOperationTerminalFrameV2
    | ComposerOperationProgressFrameV2 | ComposerOperationHeartbeatFrameV2,
    Field(discriminator="event"),
]


def encode_composer_operation_frame_v2(frame: ComposerOperationFrameV2) -> bytes:
    exact = type(frame).model_validate(frame.model_dump(mode="python"), strict=True)
    encoded = f"event: {exact.event}\ndata: {exact.model_dump_json()}\n\n".encode()
    if len(encoded) > COMPOSER_OPERATION_STREAM_FRAME_MAX_BYTES:
        raise ValueError("Composer operation stream frame exceeds encoded byte bound")
    return encoded
```

- [ ] Run GREEN with `c7-1-green` and the exact RED command; require native 0.
- [ ] Run affected attribute/masquerade/contracts gates from the integration command
  packet, inspect changes, then commit only this new module and its test with
  `feat(composer): define closed continuation transport projections`.

```bash
cd "$COMPOSER_WORKTREE" && git status --short
cd "$COMPOSER_WORKTREE" && git add -- src/elspeth/web/sessions/composer_continuation_dtos.py tests/unit/web/sessions/test_composer_continuation_dtos.py
cd "$COMPOSER_WORKTREE" && git diff --cached --name-only
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" scripts/branch-safety-check.sh --intent commit
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" git commit -m 'feat(composer): define closed continuation transport projections'
```

### Task C7.2: Header selection and immutable lifecycle hash

**Create:** `src/elspeth/web/sessions/composer_operation_versions.py`.
**Modify:** `src/elspeth/web/sessions/composer_operations.py`.
**Create:** `tests/unit/web/sessions/test_composer_operation_versions.py`.

- [ ] Write this complete test file.

```python
from uuid import UUID

import pytest
from fastapi import HTTPException

from elspeth.contracts.hashing import stable_hash
from elspeth.web.sessions.composer_operation_versions import (
    operation_request_hash_for_version, parse_operation_version, require_read_version,
)
from elspeth.web.sessions.composer_operations import composer_operation_request_hash
from elspeth.web.sessions.schemas import SendMessageRequest


def test_header_is_closed_and_v1_hash_bytes_are_preserved() -> None:
    session_id = UUID("11111111-1111-4111-8111-111111111111")
    body = SendMessageRequest(operation_id="22222222-2222-4222-8222-222222222222", content="hello", state_id=None)
    old = composer_operation_request_hash(session_id=session_id, kind="compose_message", request=body)
    assert parse_operation_version(None) == parse_operation_version("1") == 1
    assert parse_operation_version("2") == 2
    assert operation_request_hash_for_version(version=1, session_id=session_id, kind="compose_message", request=body) == old
    assert operation_request_hash_for_version(version=2, session_id=session_id, kind="compose_message", request=body) == stable_hash({
        "schema": "composer-operation-request.v2", "lifecycle_version": 2, "v1_request_hash": old,
    })
    for header in ["0", "3", "02", "2, 1", "", " 2"]:
        with pytest.raises(HTTPException) as caught:
            parse_operation_version(header)
        assert caught.value.status_code == 406
    assert require_read_version(requested=2, stored=1, operation_id=str(body.operation_id)) == 1
    assert require_read_version(requested=2, stored=2, operation_id=str(body.operation_id)) == 2
    with pytest.raises(HTTPException) as caught:
        require_read_version(requested=1, stored=2, operation_id=str(body.operation_id))
    assert caught.value.status_code == 409
    assert caught.value.detail["required_version"] == 2
```

- [ ] After adding the importable version/hash packet below, temporarily make
  `parse_operation_version` return `2` for every argument. Run the new test
  module with `-n 0 -q`; require native 1 for the default-v1/malformed-header
  assertions. Restore the exact closed parser before GREEN. Missing-module
  or collection failures do not establish the protocol behavior.
- [ ] In `composer_operations.py`, add `stable_hash` to the existing hashing
  imports and add this complete helper immediately after the unchanged v1
  `composer_operation_request_hash`. The core v2 row validator calls this
  same helper, avoiding an import cycle through the HTTP version module.

```python
def composer_operation_v2_request_hash(*, session_id: UUID, kind: ComposerOperationKind, request: BaseModel) -> str:
    old_hash = composer_operation_request_hash(session_id=session_id, kind=kind, request=request)
    return stable_hash({"schema": "composer-operation-request.v2", "lifecycle_version": 2, "v1_request_hash": old_hash})
```

- [ ] Create this complete version module.

```python
"""HTTP lifecycle selection; request bodies and v1 hash bytes are unchanged."""

from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel

from elspeth.web.sessions.composer_operations import (
    ComposerOperationKind, composer_operation_request_hash, composer_operation_v2_request_hash,
)

type ComposerOperationVersion = Literal[1, 2]


def parse_operation_version(value: str | None) -> ComposerOperationVersion:
    if value is None or value == "1":
        return 1
    if value == "2":
        return 2
    raise HTTPException(status_code=406, detail={"error_type": "composer_operation_version_unsupported"},
                        headers={"Cache-Control": "no-store"})


def operation_request_hash_for_version(
    *, version: ComposerOperationVersion, session_id: UUID, kind: ComposerOperationKind, request: BaseModel
) -> str:
    if version == 1:
        return composer_operation_request_hash(session_id=session_id, kind=kind, request=request)
    return composer_operation_v2_request_hash(session_id=session_id, kind=kind, request=request)


def require_read_version(
    *, requested: ComposerOperationVersion, stored: ComposerOperationVersion, operation_id: str
) -> ComposerOperationVersion:
    if stored == 2 and requested == 1:
        raise HTTPException(status_code=409, detail={
            "error_type": "composer_operation_version_required", "required_version": 2,
            "operation_id": operation_id,
        }, headers={"Cache-Control": "no-store"})
    return stored
```

- [ ] Run GREEN for this module, require native 0, and commit its three paths with
  `feat(composer): negotiate immutable operation lifecycle versions`.

```bash
cd "$COMPOSER_WORKTREE" && git status --short
cd "$COMPOSER_WORKTREE" && git add -- src/elspeth/web/sessions/composer_operations.py src/elspeth/web/sessions/composer_operation_versions.py tests/unit/web/sessions/test_composer_operation_versions.py
cd "$COMPOSER_WORKTREE" && git diff --cached --name-only
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" scripts/branch-safety-check.sh --intent commit
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" git commit -m 'feat(composer): negotiate immutable operation lifecycle versions'
```

### Task C7.3: Atomic v2 admission and complete 202 ACK

**Modify:** `operations.py:127-224`, `routes/messages.py:44-54`,
`routes/composer/compose.py:16-24` and the C7 route regression module.

Core prerequisite: existing `admit_composer_operation` and authority admission
now accept keyword-only `lifecycle_version: Literal[1, 2] = 1`,
`continuation_enabled: bool = False` and return their existing `(row, fresh)`
result. The v2 branch performs quota, reserved original
message UUID, parent, initial complete snapshot and sequence-1 queued segment
in one transaction. The route does not duplicate any of these writes.

- [ ] Extend the existing production app fixture for explicit enabled tests;
  no worker is launched, so admission is deterministically queued. Extend
  `build_composer_operation_app` with keyword-only
  `continuation_enabled: bool = False`. In `tests/helpers/composer_operations.py`,
  import `ContinuationActionBounds` and `ContinuationActionPolicy` from
  `elspeth.web.composer.continuation_clock`, then add this exact setup before
  its existing `WebSettings` construction.

```python
action_bounds = None
if continuation_enabled:
    provider_action = ContinuationActionPolicy(
        preparation=1.0, guard=5.0, quota=5.0, physical=120.0, retention=5.0,
    )
    local_action = ContinuationActionPolicy(
        preparation=1.0, guard=None, quota=None, physical=5.0, retention=1.0,
    )
    action_bounds = ContinuationActionBounds(
        primary=provider_action, advisor=provider_action, title=provider_action,
        read=local_action, session_sql=local_action, blob=local_action, external=local_action,
    )
```

  Add these exact keywords to the existing settings constructor. Existing
  callers remain disabled and have no new reserve requirement.

```python
composer_continuation_enabled=continuation_enabled,
composer_continuation_settlement_reserve_seconds=25.0 if continuation_enabled else None,
composer_continuation_action_bounds=action_bounds,
```


- [ ] Append this test before changing admission.

```python
@pytest.mark.asyncio
async def test_v2_replay_keeps_one_parent_and_header_body_version_conflicts(tmp_path) -> None:
    setup = await build_composer_operation_app(tmp_path, continuation_enabled=True)
    operation_id = str(uuid4())
    body = message_body("immutable body", operation_id=operation_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app), base_url="http://test",
        headers={"Authorization": f"Bearer {setup.token}"},
    ) as client:
        path = f"/api/sessions/{setup.session_id}/messages"
        first = await client.post(path, json=body, headers={"Composer-Operation-Version": "2"})
        replay = await client.post(path, json=body, headers={"Composer-Operation-Version": "2"})
        assert first.status_code == replay.status_code == 202
        first_body, replay_body = first.json(), replay.json()
        first_body["segment"].pop("observed_at")
        replay_body["segment"].pop("observed_at")
        assert first_body == replay_body
        assert first.json()["protocol_version"] == 2
        assert first.json()["segment"]["sequence"] == 1
        assert first.json()["status_revision"] == 0
        conflict = await client.post(path, json=body)
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["error_type"] == "composer_operation_conflict"
        changed = await client.post(path, json={**body, "content": "different"}, headers={"Composer-Operation-Version": "2"})
        assert changed.status_code == 409
    assert setup.composer.calls == 0
```

Only derived `segment.observed_at` is removed before the replay comparison;
sequence/generation/revision/deadline must remain identical without a worker.

- [ ] Run RED for that exact node; require the absent v2 ACK to fail.
- [ ] In `operations.py`, import `SessionServiceProtocol`, the v2 DTO,
  `legacy_cancel_projection`, `parse_operation_version`,
  `require_read_version`, and `operation_request_hash_for_version`.
  Add this complete helper; read and Stop paths call it before version facts.

```python
async def _owned_operation(
    request: Request, session_id: UUID, operation_id: UUID, user: UserIdentity
) -> tuple[ComposerOperationRecord, datetime]:
    try:
        await _verify_session_ownership(session_id, user, request)
    except HTTPException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail,
                            headers={**(exc.headers or {}), "Cache-Control": "no-store"}) from exc
    row, now = await run_sync_in_worker(
        _authority(request).get_with_database_now, session_id=session_id, operation_id=str(operation_id)
    )
    if row is None or row.actor_user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Operation not found", headers={"Cache-Control": "no-store"})
    return row, now


async def _snapshot_v2(
    request: Request, session_id: UUID, operation_id: UUID, user: UserIdentity
) -> ComposerOperationV2Snapshot:
    service = cast(SessionServiceProtocol, request.app.state.session_service)
    snapshot = await run_sync_in_worker(
        service.get_composer_operation_v2_snapshot, session_id=session_id,
        operation_id=str(operation_id), actor_user_id=user.user_id,
        auth_provider_type=request.app.state.settings.auth_provider,
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Operation not found", headers={"Cache-Control": "no-store"})
    return snapshot
```

- [ ] In `admit`, add a `response: Response` parameter after `kind` and use
  response union `ComposerOperationAcceptedResponse | ComposerOperationV2Snapshot`.
  Immediately after ownership verification insert the following code.

```python
version = parse_operation_version(request.headers.get("Composer-Operation-Version"))
settings = request.app.state.settings
response.headers["Composer-Operation-Version"] = str(version)
response.headers["Cache-Control"] = "no-store"
```

- [ ] In the existing admission call replace only `request_hash=...` and add
  the agreed lifecycle keyword. Preserve credential/rate-limit/error handling.

```python
request_hash=operation_request_hash_for_version(version=version, session_id=session_id, kind=kind, request=body),
lifecycle_version=version,
continuation_enabled=settings.composer_continuation_enabled,
```

The authority checks an existing operation key and exact lifecycle/body hash
before refusing a new v2 admission when `continuation_enabled` is false. That
preserves idempotent replay of an existing parent after the operator disables
new admissions. A disabled new admission raises the existing typed
`ComposerOperationPreconditionRefused` with HTTP409 and
`detail.error_type="composer_continuation_disabled"` before quota or inserts.
The route must not perform this gate before the atomic key lookup.

- [ ] After the existing worker notification, before constructing the v1 ACK,
  insert the following complete v2 return.

```python
if version == 2:
    return await _snapshot_v2(request, session_id, UUID(row.operation_id), user)
```

- [ ] In each POST route add `response: Response`, import `Response` and
  `ComposerOperationV2Snapshot`, change decorator/return annotation to the
  explicit response union, and forward the response argument as follows.

```python
# send_message body, routes/messages.py
body = await parse_message_body(request, session_id, user)
return await admit(request, session_id, body, user, rate_limiter, "compose_message", response)

# recompose body, routes/composer/compose.py
body = await parse_recompose_body(request, session_id, user)
return await admit(request, session_id, body, user, rate_limiter, "compose_recompose", response)
```

- [ ] Run the admission regression, old v1 packet and core atomic-admission
  negative controls; require native 0. Replay of a terminal parent must still
  return 202 with the actual terminal v2 snapshot, not a new queued segment.
- [ ] Commit these exact changes with
  `feat(composer): admit continuation through immutable versioned requests`.

```bash
cd "$COMPOSER_WORKTREE" && git status --short
cd "$COMPOSER_WORKTREE" && git add -- src/elspeth/web/sessions/routes/composer/operations.py src/elspeth/web/sessions/routes/messages.py src/elspeth/web/sessions/routes/composer/compose.py tests/helpers/composer_operations.py tests/unit/web/sessions/routes/composer/test_operation_outcomes.py
cd "$COMPOSER_WORKTREE" && git diff --cached --name-only
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" scripts/branch-safety-check.sh --intent commit
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" git commit -m 'feat(composer): admit continuation through immutable versioned requests'
```

### Task C7.4: Bind authenticated reads and Stop to the real core facets

**Modify:** `src/elspeth/web/sessions/routes/composer/operations.py:252-316`.
**Modify:** `tests/unit/web/sessions/routes/composer/test_operation_outcomes.py`.

- [ ] Append this regression test using the enabled fixture from C7.3.

```python
@pytest.mark.asyncio
async def test_old_stop_persists_before_version_required_read(tmp_path) -> None:
    setup = await build_composer_operation_app(tmp_path, continuation_enabled=True)
    operation_id = str(uuid4())
    endpoint = f"/api/sessions/{setup.session_id}/operations/{operation_id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app), base_url="http://test",
        headers={"Authorization": f"Bearer {setup.token}"},
    ) as client:
        admitted = await client.post(
            f"/api/sessions/{setup.session_id}/messages",
            json=message_body("private-continuation-marker", operation_id=operation_id),
            headers={"Composer-Operation-Version": "2"},
        )
        assert admitted.status_code == 202, admitted.text
        assert admitted.headers["composer-operation-version"] == "2"
        rejected = await client.get(endpoint)
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["error_type"] == "composer_operation_version_required"
        stopped = await client.post(f"{endpoint}/cancel")
        assert stopped.status_code in (200, 202), stopped.text
        assert stopped.headers["composer-operation-version"] == "1"
        assert stopped.json()["deadline_remaining_ms"] == 0
        assert "logical_status" not in stopped.json()
        old_followup = await client.get(endpoint)
        assert old_followup.status_code == 409
        assert old_followup.json()["detail"]["required_version"] == 2
        current = await client.get(endpoint, headers={"Composer-Operation-Version": "2"})
        assert current.status_code == 200, current.text
        assert current.json()["cancel_requested"] or (
            current.json()["logical_status"] == "failed"
            and current.json()["error"]["failure_code"] == "request_cancelled"
        )
        assert "private-continuation-marker" not in current.text
        unsupported = await client.get(endpoint, headers={"Composer-Operation-Version": "3"})
        assert unsupported.status_code == 406
    assert setup.composer.calls == 0
```

- [ ] Add this exact feature-off replay regression to the same existing route
  module after read/Stop works. It changes only this isolated app's request
  settings to simulate the new-admission gate; it does not start a worker or
  change persisted lifecycle. The production flag remains a startup setting.

```python
@pytest.mark.asyncio
async def test_disabling_new_v2_admissions_keeps_exact_replay_and_stop(tmp_path) -> None:
    setup = await build_composer_operation_app(tmp_path, continuation_enabled=True)
    operation_id = str(uuid4())
    body = message_body("held original request", operation_id=operation_id)
    headers = {"Composer-Operation-Version": "2"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app), base_url="http://test",
        headers={"Authorization": f"Bearer {setup.token}"},
    ) as client:
        path = f"/api/sessions/{setup.session_id}/messages"
        endpoint = f"/api/sessions/{setup.session_id}/operations/{operation_id}"
        admitted = await client.post(path, json=body, headers=headers)
        assert admitted.status_code == 202, admitted.text
        setup.app.state.settings = setup.app.state.settings.model_copy(update={"composer_continuation_enabled": False})
        replay = await client.post(path, json=body, headers=headers)
        assert replay.status_code == 202, replay.text
        refused = await client.post(path, json=message_body("new request"), headers=headers)
        assert refused.status_code == 409
        assert refused.json()["detail"]["error_type"] == "composer_continuation_disabled"
        read = await client.get(endpoint, headers=headers)
        assert read.status_code == 200
        stopped = await client.post(f"{endpoint}/cancel", headers=headers)
        assert stopped.status_code == 202, stopped.text
        assert stopped.json()["logical_status"] == "queued"
        assert stopped.json()["cancel_requested"] is True
        pending_replay = await client.post(path, json=body, headers=headers)
        assert pending_replay.status_code == 202
        assert pending_replay.json()["operation_id"] == operation_id
        assert pending_replay.json()["logical_status"] == "queued"
        assert pending_replay.json()["cancel_requested"] is True
        assert pending_replay.json()["segment"] is not None
    assert setup.composer.calls == 0
```

- [ ] Keep this no-worker test scoped to the durable cancellation marker and
  replay. Terminal cancellation/replay requires the real worker's settlement
  and is proved in the storage companion's C4 recovery packet; a 202 marker
  is not an immediate terminal ACK.

- [ ] Run RED with the exact test node and `-n 0 -q`. Require a failed assertion
  caused by absent v2 negotiation, not a missing fixture dependency.
- [ ] Replace `get_composer_operation` with this complete body/signature and
  change its decorator's `response_model` to the same response union.

```python
async def get_composer_operation(
    session_id: UUID,
    operation_id: UUID,
    request: Request,
    response: Response,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
) -> ComposerOperationStatusResponse | ComposerOperationV2Snapshot:
    row, now = await _owned_operation(request, session_id, operation_id, user)
    selected = require_read_version(
        requested=parse_operation_version(request.headers.get("Composer-Operation-Version")),
        stored=row.lifecycle_version, operation_id=str(operation_id),
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Composer-Operation-Version"] = str(selected)
    if selected == 2:
        return await _snapshot_v2(request, session_id, operation_id, user)
    return composer_operation_status_response(
        row, db_now=now, poll_after_ms=request.app.state.settings.composer_async_poll_after_ms
    )
```

- [ ] Replace `cancel_composer_operation` with this complete body/signature and
  change the decorator to its union. The old GET helper cannot be called after
  a v2 Stop: its required-version refusal would hide a successfully persisted
  cancellation from the caller.

```python
async def cancel_composer_operation(
    session_id: UUID,
    operation_id: UUID,
    request: Request,
    response: Response,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
) -> ComposerOperationStatusResponse | ComposerOperationV2Snapshot:
    row, _now = await _owned_operation(request, session_id, operation_id, user)
    requested = parse_operation_version(request.headers.get("Composer-Operation-Version"))
    response.headers["Cache-Control"] = "no-store"
    if row.lifecycle_version == 2:
        service = cast(SessionServiceProtocol, request.app.state.session_service)
        cancelled = await run_sync_in_worker(
            service.request_composer_operation_v2_cancel, session_id=session_id,
            operation_id=str(operation_id), actor_user_id=user.user_id,
            auth_provider_type=request.app.state.settings.auth_provider,
        )
        if cancelled is None:
            raise HTTPException(status_code=404, detail="Operation not found", headers={"Cache-Control": "no-store"})
        snapshot, cancelled_at = cancelled
        live = snapshot.logical_status in ("queued", "running")
        response.status_code = 202 if live else 200
        response.headers["Composer-Operation-Version"] = str(requested)
        if live:
            request.app.state.composer_async_worker.signal_local_cancel(
                session_id=session_id, operation_id=str(operation_id)
            )
        return snapshot if requested == 2 else legacy_cancel_projection(snapshot, cancelled_at=cancelled_at)
    await run_sync_in_worker(
        _authority(request).request_cancel, session_id=session_id,
        operation_id=str(operation_id), cancelled_failure=request_cancelled_error,
    )
    current, now = await _owned_operation(request, session_id, operation_id, user)
    result = composer_operation_status_response(
        current, db_now=now, poll_after_ms=request.app.state.settings.composer_async_poll_after_ms
    )
    response.headers["Composer-Operation-Version"] = "1"
    response.status_code = 202 if result.status in ("queued", "running") else 200
    if result.status in ("queued", "running"):
        request.app.state.composer_async_worker.signal_local_cancel(session_id=session_id, operation_id=str(operation_id))
    return result
```

- [ ] Run GREEN for the exact test and the entire affected v1 route/DTO packet.
  Require native 0. With no worker, the queued marker regression requires
  202; actual worker settlement and its terminal response are separate checks.
- [ ] Commit the three owned route/helper-test paths after focused gates with
  `feat(composer): read and stop versioned logical operations`.

```bash
cd "$COMPOSER_WORKTREE" && git status --short
cd "$COMPOSER_WORKTREE" && git add -- src/elspeth/web/sessions/routes/composer/operations.py tests/helpers/composer_operations.py tests/unit/web/sessions/routes/composer/test_operation_outcomes.py
cd "$COMPOSER_WORKTREE" && git diff --cached --name-only
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" scripts/branch-safety-check.sh --intent commit
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" git commit -m 'feat(composer): read and stop versioned logical operations'
```

### Task C7.5: Stream lifecycle negotiation and complete v2 frames

**Modify:** `operations.py:319-469`, `web/composer_stream.py` only if its
existing response cannot carry a selected header without changing bounds.
**Extend:** `tests/unit/web/test_composer_stream.py`,
`tests/unit/web/sessions/test_composer_continuation_dtos.py`.

- [ ] Add this regression to
  `tests/unit/web/sessions/routes/composer/test_operation_outcomes.py`, which
  already imports the exact fixture, pytest, httpx and `uuid4`. Run it before
  stream negotiation is added; the absent old-client refusal must fail.

```python
@pytest.mark.asyncio
async def test_old_reader_cannot_open_v2_event_stream(tmp_path) -> None:
    setup = await build_composer_operation_app(tmp_path, continuation_enabled=True)
    operation_id = str(uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app), base_url="http://test",
        headers={"Authorization": f"Bearer {setup.token}"},
    ) as client:
        admitted = await client.post(
            f"/api/sessions/{setup.session_id}/messages",
            json=message_body("stream-private-marker", operation_id=operation_id),
            headers={"Composer-Operation-Version": "2"},
        )
        assert admitted.status_code == 202
        old_stream = await client.get(f"/api/sessions/{setup.session_id}/operations/{operation_id}/stream")
        assert old_stream.status_code == 409
        assert old_stream.json()["detail"]["required_version"] == 2
        assert old_stream.headers.get("content-type", "").split(";")[0] != "text/event-stream"
        assert "stream-private-marker" not in old_stream.text
    assert setup.composer.calls == 0
```

- [ ] Add the following complete framing/bundle test to the new DTO module.

```python
def test_v2_frame_is_exact_and_revision_bound() -> None:
    from elspeth.web.sessions.composer_continuation_dtos import (
        ComposerOperationHeartbeatFrameV2, ComposerOperationStatusFrameV2,
        encode_composer_operation_frame_v2,
    )
    now = datetime(2026, 10, 20, tzinfo=UTC)
    snapshot = ComposerOperationV2Snapshot(
        operation_id="22222222-2222-4222-8222-222222222222", kind="compose_message",
        logical_status="queued", phase="queued", status_revision=0, poll_after_ms=1000,
        cancel_requested=False, result=None, error=None,
        segment=ComposerOperationSegmentV2(sequence=1, generation=1, state="queued",
            observed_at=now, deadline_at=None, remaining_ms=None, rollover_pending=False),
    )
    common = dict(session_id="11111111-1111-4111-8111-111111111111",
                  operation_id=snapshot.operation_id, sequence=0, status_revision=0)
    frame = ComposerOperationStatusFrameV2(**common, payload=snapshot)
    wire = encode_composer_operation_frame_v2(frame)
    assert wire.startswith(b"event: status\ndata: ") and wire.endswith(b"\n\n")
    assert b'"protocol_version":2' in wire and b'"status_revision":0' in wire
    assert b"claim_token" not in wire and b"checkpoint" not in wire
    with pytest.raises(ValidationError):
        ComposerOperationStatusFrameV2(**{**common, "status_revision": 1}, payload=snapshot)
    with pytest.raises(ValidationError):
        ComposerOperationHeartbeatFrameV2(**common, payload={"private": "marker"})
```

- [ ] Before acquiring the current stream permit, perform an owned lifecycle
  read and select the protocol. This must happen before ASGI starts SSE.
  The current frame-by-frame auth checks and resistant-send custody remain.

```python
owned, _now = await _owned_operation(request, session_id, operation_id, user)
selected = require_read_version(
    requested=parse_operation_version(request.headers.get("Composer-Operation-Version")),
    stored=owned.lifecycle_version, operation_id=str(operation_id),
)
```

- [ ] In the `frames` loop, after the current authenticated authority read and
  before the v1 terminal/status branches, insert this version branch. It uses
  the existing progress projection rather than private checkpoints. Existing
  `pending_progress_binding` authorization is preserved exactly.

  Add a distinct local `pending_progress_binding_v2` alongside the existing
  v1 binding; its declaration is:

```python
pending_progress_binding_v2: tuple[str, int, str, int, int] | None = None
```

  Add it to `frames`'s existing nonlocal declaration and reset it to `None`
  alongside `pending_progress_binding` before building every status frame.
  In `authorize`, after the old v1 pending-binding check and before
  `return may_send()`, insert this exact complete v2 check:

```python
if pending_progress_binding_v2 is not None:
    fence_id, fence_epoch, request_token, segment_sequence, segment_generation = pending_progress_binding_v2
    if (
        job.session_operation_id != fence_id or job.session_operation_epoch != fence_epoch
        or job.current_segment_sequence != segment_sequence or job.ownership_generation != segment_generation
    ):
        return False
    current_progress = await _get_composer_progress_registry(request).get_for_operation(
        session_id=str(session_id), user_id=user.user_id, operation_id=str(operation_id),
        session_operation_id=fence_id, session_operation_epoch=fence_epoch,
    )
    if current_progress is None or current_progress.request_token != request_token:
        return False
```

```python
if selected == 2:
    public = await _snapshot_v2(request, session_id, operation_id, user)
    common = dict(session_id=str(session_id), operation_id=str(operation_id),
                  sequence=sequence, status_revision=public.status_revision)
    if public.logical_status in ("completed", "failed"):
        yield encode_composer_operation_frame_v2(ComposerOperationTerminalFrameV2(**common, payload=public))
        return
    yield encode_composer_operation_frame_v2(ComposerOperationStatusFrameV2(**common, payload=public))
    sequence += 1
    segment = public.segment
    if segment is None:
        raise AuditIntegrityError("Live public snapshot has no segment")
    same_segment = (
        snapshot.current_segment_sequence == segment.sequence
        and snapshot.ownership_generation == segment.generation
    )
    if same_segment and snapshot.session_operation_id is not None and snapshot.session_operation_epoch is not None:
        progress = await _get_composer_progress_registry(request).get_for_operation(
            session_id=str(session_id), user_id=user.user_id, operation_id=str(operation_id),
            session_operation_id=snapshot.session_operation_id,
            session_operation_epoch=snapshot.session_operation_epoch,
        )
        if progress is not None:
            if progress.request_token is None:
                raise AuditIntegrityError("Operation progress has no request custody token")
            pending_progress_binding_v2 = (
                snapshot.session_operation_id, snapshot.session_operation_epoch, progress.request_token,
                segment.sequence, segment.generation,
            )
            yield encode_composer_operation_frame_v2(ComposerOperationProgressFrameV2(
                session_id=str(session_id), operation_id=str(operation_id), sequence=sequence,
                status_revision=public.status_revision,
                payload=ComposerOperationProgressPayloadV2(
                    segment_sequence=segment.sequence, segment_generation=segment.generation,
                    session_operation_id=snapshot.session_operation_id,
                    session_operation_epoch=snapshot.session_operation_epoch,
                    request_token=progress.request_token, request_id=progress.request_id,
                    phase=progress.phase, headline=cast(str, scrub_credential_material(progress.headline)),
                    evidence=tuple(cast(str, scrub_credential_material(item)) for item in progress.evidence),
                    likely_next=cast(str, scrub_credential_material(progress.likely_next)),
                    reason=progress.reason, updated_at=progress.updated_at,
                ),
            ))
            sequence += 1
    if asyncio.get_running_loop().time() - last_heartbeat >= 10:
        yield encode_composer_operation_frame_v2(ComposerOperationHeartbeatFrameV2(
            session_id=str(session_id), operation_id=str(operation_id),
            sequence=sequence, status_revision=public.status_revision,
        ))
        sequence += 1
        last_heartbeat = asyncio.get_running_loop().time()
    await asyncio.sleep(1.0)
    continue
```

The explicit `same_segment` test handles a rollover between the old row read
and complete public read. The final frame authorization then compares the
captured generation and fence to the latest row before sending; a stale
progress frame closes that physical subscription and the durable reader
reconnects. No old progress is stamped with a new segment identity.
- [ ] Replace the final one-line response return with this exact response
  construction. `BoundedComposerStreamResponse` already allows header edits
  before ASGI delivery; no change to its timeout/permit implementation is needed.

```python
response = BoundedComposerStreamResponse(
    frames=frames(), authorize=authorize, permits=permits, permit=permit, may_send=may_send
)
response.headers["Composer-Operation-Version"] = str(selected)
return response
```

- [ ] Run DTO, v1 stream, worker-custody and authenticated reconnect tests;
  require native 0. Existing v1 clients must receive 409 before SSE headers for
  v2 parents. A requested v2 reader of a v1 parent must receive header 1 and
  exact old frames. No new frame may follow auth revocation or stale authority.
- [ ] Commit with `feat(composer): stream closed continuation status snapshots`.

```bash
cd "$COMPOSER_WORKTREE" && git status --short
cd "$COMPOSER_WORKTREE" && git add -- src/elspeth/web/sessions/routes/composer/operations.py tests/unit/web/sessions/test_composer_continuation_dtos.py tests/unit/web/test_composer_stream.py tests/unit/web/sessions/routes/composer/test_operation_outcomes.py
cd "$COMPOSER_WORKTREE" && git diff --cached --name-only
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" scripts/branch-safety-check.sh --intent commit
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" git commit -m 'feat(composer): stream closed continuation status snapshots'
```

### Task C8.1: Explicit frontend v2 types and strict snapshot/frame decoding

**Modify:** `src/elspeth/web/frontend/src/types/composerOperations.ts` and
`src/elspeth/web/frontend/src/api/composerOperationDecoder.ts`.
**Create:** `src/elspeth/web/frontend/src/api/composerOperationV2Decoder.ts` and its `.test.ts`.
All `src/elspeth/web/frontend/` paths below have prefix `src/elspeth/web/`.

- [ ] Append these exact types. Existing `ComposerOperationSnapshot` and frame
  types remain v1; do not weaken their closed decoder.

```typescript
export type ComposerOperationVersion = 1 | 2;
export type ComposerContinuationPhase = "queued" | "executing" | "continuation_queued" | "reconciling" | "retry_waiting" | "settled";
export interface ComposerOperationSegmentV2 {
  sequence: number; generation: number; state: "queued" | "running" | "interrupted";
  observed_at: string; deadline_at: string | null; remaining_ms: number | null;
  rollover_pending: boolean;
}
export interface ComposerOperationSnapshotV2 {
  protocol_version: 2; operation_id: string; kind: OperationKind;
  logical_status: OperationStatus; phase: ComposerContinuationPhase;
  status_revision: number; poll_after_ms: number; cancel_requested: boolean;
  segment: ComposerOperationSegmentV2 | null;
  result: MessageWithStateResponse | null; error: ComposerOperationError | null;
}
export type ComposerOperationFrameV2 = {
  schema_version: "composer-operation-stream.v2"; session_id: string;
  operation_id: string; sequence: number; status_revision: number;
} & (
  | { event: "heartbeat" }
  | { event: "status" | "terminal"; payload: ComposerOperationSnapshotV2 }
  | { event: "progress"; payload: Extract<ComposerOperationFrame, { event: "progress" }>["payload"] & { segment_sequence: number; segment_generation: number } }
);
export type ComposerOperationObservedSnapshot = ComposerOperationSnapshot | ComposerOperationSnapshotV2;
```

- [ ] In the existing decoder change only these two declarations to exported
  functions, keeping their complete bodies unchanged:

```typescript
export function timestamp(value: unknown): string
export function decodeError(value: unknown, sessionId: string): ComposerOperationError
```

- [ ] Create this complete test file before the new decoder module.

```typescript
import { expect, it } from "vitest";
import { decodeComposerOperationSnapshotV2, decodeComposerOperationFrameV2 } from "./composerOperationV2Decoder";

const sid = "11111111-1111-4111-8111-111111111111";
const id = "22222222-2222-4222-8222-222222222222";
const queued = {
  protocol_version: 2, operation_id: id, kind: "compose_message", logical_status: "queued",
  phase: "queued", status_revision: 0, poll_after_ms: 1000, cancel_requested: false,
  segment: { sequence: 1, generation: 1, state: "queued", observed_at: "2026-10-20T00:00:00Z",
    deadline_at: null, remaining_ms: null, rollover_pending: false }, result: null, error: null,
};
it("admits the exact backend snapshot and rejects extra/private/bundle corruption", () => {
  expect(decodeComposerOperationSnapshotV2(queued, sid, id)).toEqual(queued);
  for (const changes of [
    { private_checkpoint: "marker" }, { status_revision: true }, { phase: "settled" },
    { operation_id: sid }, { segment: null }, { logical_status: "cancelled" },
    { segment: { ...queued.segment, claim_token: "marker" } },
    { segment: { ...queued.segment, generation: 0 } },
    { segment: { ...queued.segment, observed_at: "2026-10-20" } },
    { segment: { ...queued.segment, state: "running" } },
    { segment: { ...queued.segment, deadline_at: "2026-10-20T00:00:30Z" } },
  ]) expect(() => decodeComposerOperationSnapshotV2({ ...queued, ...changes }, sid, id)).toThrow();
});
it("binds snapshot status frames and forbids heartbeat payloads", () => {
  const common = { schema_version: "composer-operation-stream.v2", session_id: sid,
    operation_id: id, sequence: 0, status_revision: 0 };
  expect(decodeComposerOperationFrameV2({ ...common, event: "status", payload: queued }, sid, id).event).toBe("status");
  expect(() => decodeComposerOperationFrameV2({ ...common, status_revision: 1, event: "status", payload: queued }, sid, id)).toThrow();
  expect(() => decodeComposerOperationFrameV2({ ...common, event: "terminal", payload: queued }, sid, id)).toThrow();
  expect(() => decodeComposerOperationFrameV2({ ...common, event: "heartbeat", payload: {} }, sid, id)).toThrow();
  expect(() => decodeComposerOperationFrameV2({ ...common, event: "answer_delta", payload: "marker" }, sid, id)).toThrow();
});
```

- [ ] After the decoder packet below is importable, temporarily remove its
  unknown-key rejection and run the behavioral RED using
  `npm --prefix "$PWD/src/elspeth/web/frontend" test -- src/api/composerOperationV2Decoder.test.ts`.
  Require the private-field/unknown-key assertion to fail, then restore the
  rejection and obtain GREEN. A missing module is only setup failure.

```typescript
import {
  canonicalUuid, decodeComposerOperationFrame, decodeComposerResult, decodeError,
  exactRecord, integer, operationKind, operationStatus, timestamp,
  ComposerOperationDecodeError,
} from "./composerOperationDecoder";
import type {
  ComposerContinuationPhase, ComposerOperationFrameV2,
  ComposerOperationSegmentV2, ComposerOperationSnapshotV2,
} from "@/types/composerOperations";

function segment(value: unknown): ComposerOperationSegmentV2 {
  const r = exactRecord(value, ["sequence", "generation", "state", "observed_at", "deadline_at", "remaining_ms", "rollover_pending"]);
  if (r.state !== "queued" && r.state !== "running" && r.state !== "interrupted") throw new ComposerOperationDecodeError("Unknown segment state");
  if (typeof r.rollover_pending !== "boolean") throw new ComposerOperationDecodeError("Invalid rollover flag");
  const deadline_at = r.deadline_at === null ? null : timestamp(r.deadline_at);
  const remaining_ms = r.remaining_ms === null ? null : integer(r.remaining_ms);
  if (r.state === "running" ? deadline_at === null || remaining_ms === null : deadline_at !== null || remaining_ms !== null) throw new ComposerOperationDecodeError("Invalid segment deadline bundle");
  return { sequence: integer(r.sequence, 1), generation: integer(r.generation, 1), state: r.state,
    observed_at: timestamp(r.observed_at), deadline_at, remaining_ms, rollover_pending: r.rollover_pending };
}
export function decodeComposerOperationSnapshotV2(value: unknown, sessionId: string, operationId: string): ComposerOperationSnapshotV2 {
  const r = exactRecord(value, ["protocol_version", "operation_id", "kind", "logical_status", "phase", "status_revision", "poll_after_ms", "cancel_requested", "segment", "result", "error"]);
  if (r.protocol_version !== 2 || canonicalUuid(r.operation_id) !== operationId) throw new ComposerOperationDecodeError("Snapshot version/identity mismatch");
  const status = operationStatus(r.logical_status);
  const phases: readonly ComposerContinuationPhase[] = ["queued", "executing", "continuation_queued", "reconciling", "retry_waiting", "settled"];
  const phase = phases.find((item) => item === r.phase);
  if (phase === undefined || typeof r.cancel_requested !== "boolean") throw new ComposerOperationDecodeError("Invalid snapshot phase/cancel flag");
  const poll_after_ms = integer(r.poll_after_ms, 100);
  if (poll_after_ms > 60000) throw new ComposerOperationDecodeError("Invalid poll interval");
  const currentSegment = r.segment === null ? null : segment(r.segment);
  const result = r.result === null ? null : decodeComposerResult(r.result, sessionId);
  const error = r.error === null ? null : decodeError(r.error, sessionId);
  if (status === "completed" || status === "failed") {
    if (phase !== "settled" || currentSegment !== null) throw new ComposerOperationDecodeError("Terminal segment bundle invalid");
    if (status === "completed" ? result === null || error !== null || r.cancel_requested : error === null || result !== null) throw new ComposerOperationDecodeError("Terminal result bundle invalid");
  } else {
    if (currentSegment === null || result !== null || error !== null) throw new ComposerOperationDecodeError("Live result bundle invalid");
    if (status === "queued" ? phase !== "queued" : phase === "queued" || phase === "settled") throw new ComposerOperationDecodeError("Live phase bundle invalid");
  }
  if (result?.message.operation_id != null && result.message.operation_id !== operationId) throw new ComposerOperationDecodeError("Terminal operation binding mismatch");
  if (result?.recovery_notice != null && result.recovery_notice.operation_id !== operationId) throw new ComposerOperationDecodeError("Recovery operation binding mismatch");
  return { protocol_version: 2, operation_id: operationId, kind: operationKind(r.kind),
    logical_status: status, phase, status_revision: integer(r.status_revision), poll_after_ms,
    cancel_requested: r.cancel_requested, segment: currentSegment, result, error };
}
export function decodeComposerOperationFrameV2(value: unknown, sessionId: string, operationId: string): ComposerOperationFrameV2 {
  const keys = ["schema_version", "session_id", "operation_id", "sequence", "status_revision", "event"];
  const r = exactRecord(value, [...keys, "payload"], keys);
  if (r.schema_version !== "composer-operation-stream.v2" || canonicalUuid(r.session_id) !== sessionId || canonicalUuid(r.operation_id) !== operationId) throw new ComposerOperationDecodeError("Stream scope/version mismatch");
  const common = { schema_version: "composer-operation-stream.v2" as const, session_id: sessionId,
    operation_id: operationId, sequence: integer(r.sequence), status_revision: integer(r.status_revision) };
  if (r.event === "heartbeat") {
    if ("payload" in r) throw new ComposerOperationDecodeError("Heartbeat payload forbidden");
    return { ...common, event: "heartbeat" };
  }
  if (r.event === "status" || r.event === "terminal") {
    const payload = decodeComposerOperationSnapshotV2(r.payload, sessionId, operationId);
    const terminal = payload.logical_status === "completed" || payload.logical_status === "failed";
    if ((r.event === "terminal") !== terminal || payload.status_revision !== common.status_revision) throw new ComposerOperationDecodeError("Frame status/revision mismatch");
    return { ...common, event: r.event, payload };
  }
  if (r.event !== "progress") throw new ComposerOperationDecodeError("Unknown stream event");
  const p = exactRecord(r.payload, ["session_operation_id", "session_operation_epoch", "request_token", "request_id", "phase", "headline", "evidence", "likely_next", "reason", "updated_at", "segment_sequence", "segment_generation"]);
  const { segment_sequence, segment_generation, ...existing } = p;
  const admitted = decodeComposerOperationFrame({ schema_version: "composer-operation-stream.v1",
    session_id: sessionId, operation_id: operationId, sequence: common.sequence,
    event: "progress", payload: existing }, sessionId, operationId);
  if (admitted.event !== "progress") throw new ComposerOperationDecodeError("Progress decoder disagreement");
  return { ...common, event: "progress", payload: { ...admitted.payload,
    segment_sequence: integer(segment_sequence, 1), segment_generation: integer(segment_generation, 1) } };
}
```

- [ ] Run GREEN for both v1/v2 decoder files and
  `npm --prefix "$PWD/src/elspeth/web/frontend" run typecheck`;
  require native 0. Commit only these paths with
  `feat(composer): decode versioned snapshots and stream frames strictly`.

```bash
cd "$COMPOSER_WORKTREE" && git status --short
cd "$COMPOSER_WORKTREE" && git add -- src/elspeth/web/frontend/src/types/composerOperations.ts src/elspeth/web/frontend/src/api/composerOperationDecoder.ts src/elspeth/web/frontend/src/api/composerOperationV2Decoder.ts src/elspeth/web/frontend/src/api/composerOperationV2Decoder.test.ts
cd "$COMPOSER_WORKTREE" && git diff --cached --name-only
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" scripts/branch-safety-check.sh --intent commit
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" git commit -m 'feat(composer): decode versioned snapshots and stream frames strictly'
```

### Task C8.2: Durable revision reducer and deadline/notice rules

**Create:** `src/elspeth/web/frontend/src/api/composerContinuationState.ts` and `.test.ts`.

- [ ] Write the following complete test file.

```typescript
import { expect, it } from "vitest";
import { ComposerContinuationState } from "./composerContinuationState";
import type { ComposerOperationSnapshotV2 } from "@/types/composerOperations";

const id = "22222222-2222-4222-8222-222222222222";
function live(sequence = 1, generation = 1, revision = 1): ComposerOperationSnapshotV2 {
  return { protocol_version: 2, operation_id: id, kind: "compose_message", logical_status: "running",
    phase: "executing", status_revision: revision, poll_after_ms: 1000, cancel_requested: false,
    result: null, error: null, segment: { sequence, generation, state: "running",
      observed_at: "2026-10-20T00:00:00Z", deadline_at: "2026-10-20T00:05:00Z",
      remaining_ms: 300000, rollover_pending: false } };
}
it("suppresses historical notices and announces each newly observed successor once", () => {
  const state = new ComposerContinuationState();
  expect(state.accept(live(3, 1, 8)).successorStarted).toBe(false);
  expect(state.accept(live(4, 1, 9)).successorStarted).toBe(true);
  expect(state.accept(live(4, 1, 9)).successorStarted).toBe(false);
  expect(state.accept(live(4, 2, 10)).successorStarted).toBe(false);
  expect(state.accept(live(3, 1, 8)).accepted).toBe(false);
});
it("rejects equal revision disagreement and never extends a running generation deadline", () => {
  const state = new ComposerContinuationState();
  const initial = live(); state.accept(initial);
  expect(state.accept({ ...initial, segment: { ...initial.segment!, observed_at: "2026-10-20T00:00:01Z", remaining_ms: 299000 } }).accepted).toBe(true);
  expect(state.accept({ ...initial, segment: { ...initial.segment!, observed_at: "2026-10-20T00:04:59Z", remaining_ms: 1000, rollover_pending: true } }).accepted).toBe(true);
  expect(() => state.accept({ ...initial, phase: "reconciling" })).toThrow();
  expect(() => state.accept({ ...initial, status_revision: 2,
    segment: { ...initial.segment!, deadline_at: "2026-10-20T00:06:00Z" } })).toThrow();
});
```

- [ ] Run RED for the exact new file; create this complete implementation.

```typescript
import { ComposerOperationDecodeError } from "./composerOperationDecoder";
import type { ComposerOperationSnapshotV2 } from "@/types/composerOperations";

function canonical(value: unknown): string {
  return JSON.stringify(value, (_key, item: unknown) => {
    if (typeof item !== "object" || item === null || Array.isArray(item)) return item;
    const record = item as Record<string, unknown>;
    return Object.fromEntries(Object.keys(record).sort().map((key) => [key, record[key]]));
  });
}
function semantic(snapshot: ComposerOperationSnapshotV2): string {
  const current = snapshot.segment;
  return canonical({ ...snapshot, segment: current === null ? null : {
    sequence: current.sequence, generation: current.generation, state: current.state,
    deadline_at: current.deadline_at,
  } });
}
export class ComposerContinuationState {
  private last: ComposerOperationSnapshotV2 | null = null;
  private lastAnnouncedSequence = 0;
  accept(next: ComposerOperationSnapshotV2): { accepted: boolean; successorStarted: boolean } {
    const previous = this.last;
    if (previous !== null) {
      if (next.operation_id !== previous.operation_id || next.kind !== previous.kind) throw new ComposerOperationDecodeError("Observation identity changed");
      if (next.status_revision < previous.status_revision) return { accepted: false, successorStarted: false };
      if (next.status_revision === previous.status_revision && semantic(next) !== semantic(previous)) throw new ComposerOperationDecodeError("Equal revision semantic disagreement");
      const a = previous.segment, b = next.segment;
      if (a !== null && b !== null) {
        if (b.sequence < a.sequence || (b.sequence === a.sequence && b.generation < a.generation)) throw new ComposerOperationDecodeError("Segment identity moved backwards");
        if (a.sequence === b.sequence && a.generation === b.generation && a.state === "running" && b.state === "running" && a.deadline_at !== b.deadline_at) throw new ComposerOperationDecodeError("Running generation deadline changed");
      }
      if ((previous.logical_status === "completed" || previous.logical_status === "failed") && semantic(previous) !== semantic(next)) throw new ComposerOperationDecodeError("Terminal operation changed");
    }
    const segment = next.segment;
    const baseline = previous === null;
    if (baseline && segment !== null) {
      this.lastAnnouncedSequence = segment.state === "queued" ? segment.sequence - 1 : segment.sequence;
    }
    const successorStarted = !baseline && segment !== null && segment.state === "running"
      && segment.sequence > this.lastAnnouncedSequence && segment.sequence > 1;
    if (successorStarted && segment !== null) this.lastAnnouncedSequence = segment.sequence;
    this.last = next;
    return { accepted: true, successorStarted };
  }
  acceptsProgress(revision: number, sequence: number, generation: number): boolean {
    const last = this.last;
    return last !== null && last.logical_status === "running" && last.segment !== null
      && last.status_revision === revision && last.segment.sequence === sequence
      && last.segment.generation === generation;
  }
}
```

- [ ] Run GREEN, typecheck and the existing observer deadline tests; require
  native 0. Commit with `feat(composer): order continuation snapshots by durable revision`.

```bash
cd "$COMPOSER_WORKTREE" && git status --short
cd "$COMPOSER_WORKTREE" && git add -- src/elspeth/web/frontend/src/api/composerContinuationState.ts src/elspeth/web/frontend/src/api/composerContinuationState.test.ts
cd "$COMPOSER_WORKTREE" && git diff --cached --name-only
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" scripts/branch-safety-check.sh --intent commit
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" git commit -m 'feat(composer): order continuation snapshots by durable revision'
```

### Task C8.3: Immutable protocol custody and request/response dispatch

**Modify:** `types/composerOperations.ts`, `api/client.ts`,
`stores/composerOperationCustody.ts`, `config/composer.ts`, `App.tsx`,
`types/index.ts`, backend `app.py` and their existing tests.

- [ ] Add `readonly protocolVersion?: ComposerOperationVersion` to both
  `SubmittedCustody` and `ObserverCustody` common fields. Optionality is
  deliberate: historical records without the field mean version 1. Newly
  created production descriptors always record a literal selected version.
  Do not include this field in either closed request body.
- [ ] Add these exact capability functions to `config/composer.ts`.

```typescript
let admissionVersion: 1 | 2 = 1;
export function applyComposerOperationAdmissionVersion(value: unknown): void {
  if (value === undefined || value === 1) admissionVersion = 1;
  else if (value === 2) admissionVersion = 2;
  else throw new Error("Unsupported composer operation admission capability");
}
export function composerOperationAdmissionVersion(): 1 | 2 { return admissionVersion; }
```

- [ ] Add `composer_operation_admission_version?: 1 | 2` to `SystemStatus`.
  Immediately after `setSystemStatus(status)` in `App.checkHealth`, call
  `applyComposerOperationAdmissionVersion(status.composer_operation_admission_version)`
  using its explicit import from `@/config/composer`. Add this backend field
  to the existing `/api/system/status` result:

```python-expression
"composer_operation_admission_version": 2 if settings.composer_continuation_enabled else 1,
```

- [ ] In `admitDescriptor`, add `"protocolVersion"` to allowed keys but not
  required keys. After createdAt validation add exactly:

```typescript
if (record.protocolVersion !== undefined && record.protocolVersion !== 1 && record.protocolVersion !== 2) throw new Error("Unknown custody protocol version");
const version = record.protocolVersion === undefined ? {} : { protocolVersion: record.protocolVersion as 1 | 2 };
```

  Add `...version` to the existing `common` object; both returned immutable
  descriptor modes now retain the exact selected version. Add
  `protocolVersion: 2` when `attachComposerObserver` constructs a distinct
  read-only winner descriptor. A v2 reader can decode a selected v1 response;
  this never changes the refused original submitted body's version.
- [ ] In all four production descriptor constructors—`client.sendMessage`,
  `client.recompose`, `sessionStore.sendMessage`, `sessionStore.retryMessage`—
  insert this exact property after `createdAt` and add the config import:

```typescript
protocolVersion: composerOperationAdmissionVersion(),
```

- [ ] Add these two helpers to `client.ts`. Existing auth headers and error
  parsing remain authoritative. Missing response header selects 1 for a
  historical server; only the capability field can select a new v2 POST.

```typescript
function composerHeaders(version: 1 | 2, contentType?: string): Headers {
  const headers = new Headers(authHeaders(contentType));
  headers.set("Composer-Operation-Version", String(version));
  return headers;
}
function selectedComposerVersion(response: Response): 1 | 2 {
  const version = response.headers.get("Composer-Operation-Version");
  if (version === null || version === "1") return 1;
  if (version === "2") return 2;
  throw new Error("Unsupported selected composer operation version");
}
```

- [ ] Replace only these four named client functions with the complete code
  below. Import `decodeComposerOperationSnapshotV2` from the new decoder.
  Preserve every other current `client.ts` function, including candidate
  validation-check admission, storage-quota parsing and 429 classification.

```typescript
export async function submitComposerOperation(descriptor: import("@/types/composerOperations").SubmittedCustody): Promise<import("@/types/composerOperations").ComposerOperationAck | import("@/types/composerOperations").ComposerOperationSnapshotV2> {
  const path = descriptor.kind === "compose_message" ? "messages" : "recompose";
  const response = await authFetch(`/api/sessions/${descriptor.sessionId}/${path}`, {
    method: "POST", headers: composerHeaders(descriptor.protocolVersion ?? 1, "application/json"),
    body: JSON.stringify(descriptor.body), signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) {
    const body: unknown = await response.json();
    const error = apiErrorFromBody(response.status, body);
    if (typeof body === "object" && body !== null && "detail" in body && typeof body.detail === "object" && body.detail !== null && "operation_id" in body.detail && "kind" in body.detail) throw { ...error, operation_id: body.detail.operation_id, kind: body.detail.kind };
    throw error;
  }
  if (response.status !== 202) throw new Error("Composer admission must return 202");
  const selected = selectedComposerVersion(response);
  if (selected !== (descriptor.protocolVersion ?? 1)) throw new Error("Admission lifecycle changed");
  const value: unknown = await response.json();
  return selected === 2
    ? decodeComposerOperationSnapshotV2(value, descriptor.sessionId, descriptor.operationId)
    : decodeComposerOperationAck(value);
}
export async function fetchComposerOperation(sessionId: string, operationId: string, version: 1 | 2 = 1): Promise<import("@/types/composerOperations").ComposerOperationObservedSnapshot | import("@/types/composerOperations").MissingOperation> {
  const response = await authFetch(`/api/sessions/${sessionId}/operations/${operationId}`, {
    headers: composerHeaders(version), signal: AbortSignal.timeout(15000), cache: "no-store",
  });
  if (response.status === 404) {
    const body: unknown = await response.json();
    if (typeof body === "object" && body !== null && !Array.isArray(body) && Object.keys(body).length === 1 && "detail" in body) {
      if (body.detail === "Session not found") return { kind: "session_missing" };
      if (body.detail === "Operation not found") return { kind: "operation_missing" };
    }
    throw apiErrorFromBody(404, body);
  }
  const value = await parseResponse<unknown>(response);
  if (selectedComposerVersion(response) === 2) {
    if (version !== 2) throw new Error("Unsupported selected composer operation version");
    return decodeComposerOperationSnapshotV2(value, sessionId, operationId);
  }
  return decodeComposerOperationSnapshot(value, sessionId, operationId);
}
export async function cancelComposerOperation(sessionId: string, operationId: string, version: 1 | 2 = 1): Promise<import("@/types/composerOperations").ComposerOperationObservedSnapshot | import("@/types/composerOperations").MissingOperation> {
  const response = await authFetch(`/api/sessions/${sessionId}/operations/${operationId}/cancel`, {
    method: "POST", headers: composerHeaders(version), signal: AbortSignal.timeout(15000),
  });
  if (response.status === 404) {
    const body: unknown = await response.json();
    if (typeof body === "object" && body !== null && !Array.isArray(body) && Object.keys(body).length === 1 && "detail" in body) {
      if (body.detail === "Session not found") return { kind: "session_missing" };
      if (body.detail === "Operation not found") return { kind: "operation_missing" };
    }
    throw apiErrorFromBody(404, body);
  }
  const value = await parseResponse<unknown>(response);
  if (selectedComposerVersion(response) === 2) {
    if (version !== 2) throw new Error("Unsupported selected composer operation version");
    return decodeComposerOperationSnapshotV2(value, sessionId, operationId);
  }
  return decodeComposerOperationSnapshot(value, sessionId, operationId);
}
export function fetchComposerOperationStream(sessionId: string, operationId: string, signal: AbortSignal, version: 1 | 2 = 1): Promise<Response> {
  const headers = composerHeaders(version);
  headers.set("Accept", "text/event-stream");
  return authFetch(`/api/sessions/${sessionId}/operations/${operationId}/stream`, {
    headers, signal, cache: "no-store",
  });
}
```

- [ ] Append this complete capability test to `config/composer.test.ts` using
  explicit imports of its two new functions.

```typescript
it("selects capabilities only for future descriptors and defaults historical servers to v1", () => {
  applyComposerOperationAdmissionVersion(2);
  const held = { protocolVersion: composerOperationAdmissionVersion() };
  applyComposerOperationAdmissionVersion(undefined);
  expect(composerOperationAdmissionVersion()).toBe(1);
  expect(held.protocolVersion).toBe(2);
  expect(() => applyComposerOperationAdmissionVersion(3)).toThrow();
});
```

- [ ] Append these exact tests to the existing custody test module, whose
  `descriptor`, `scope`, `sid`, and imports are defined in that file. Import
  `applyComposerOperationAdmissionVersion` from `@/config/composer` and append
  `applyComposerOperationAdmissionVersion(1)` to its existing `beforeEach`.

```typescript
it.each([1, 2] as const)("rehydrates immutable requested version %s across capability changes", (protocolVersion) => {
  const submitted = { ...descriptor(), protocolVersion };
  purgeComposerCustody();
  sessionStorage.setItem(COMPOSER_CUSTODY_KEY, JSON.stringify({
    schema: "composer-operations.v1", entries: [{ foreground: submitted, unresolvedSubmissions: [] }],
  }));
  applyComposerOperationAdmissionVersion(protocolVersion === 2 ? 1 : 2);
  authenticateComposerCustody(scope);
  const restored = findComposerOperationCustody(scope, sid).foreground;
  expect(restored?.protocolVersion).toBe(protocolVersion);
  expect(restored).toEqual(submitted);
});
it("restores historical absent-version custody as v1 without rewriting its body", () => {
  const submitted = descriptor();
  purgeComposerCustody();
  sessionStorage.setItem(COMPOSER_CUSTODY_KEY, JSON.stringify({
    schema: "composer-operations.v1", entries: [{ foreground: submitted, unresolvedSubmissions: [] }],
  }));
  applyComposerOperationAdmissionVersion(2);
  authenticateComposerCustody(scope);
  const restored = findComposerOperationCustody(scope, sid).foreground;
  expect(restored?.protocolVersion ?? 1).toBe(1);
  expect(restored).toEqual(submitted);
});
```
- [ ] Run focused config/client/custody/store tests and typecheck with native
  exit recording. Preserve candidate24a's rate-limited/queue-full 429
  definitive refusal regression. Commit with
  `feat(composer): retain immutable lifecycle version in browser custody`.

```bash
cd "$COMPOSER_WORKTREE" && {
  npm --prefix "$PWD/src/elspeth/web/frontend" test -- src/config/composer.test.ts src/api/client.composerTransport.test.ts src/api/composerOperationObserver.test.ts src/stores/composerOperationCustody.test.ts src/stores/sessionStore.composerOperations.test.ts > "$COMPOSER_CLIENT_LOG_DIR/c8-3-ui.log" 2>&1
  client_check_status=$?
  cat "$COMPOSER_CLIENT_LOG_DIR/c8-3-ui.log"
  printf 'native_exit=%s\n' "$client_check_status"
  test "$client_check_status" -eq 0
}
cd "$COMPOSER_WORKTREE" && {
  npm --prefix "$PWD/src/elspeth/web/frontend" run typecheck > "$COMPOSER_CLIENT_LOG_DIR/c8-3-types.log" 2>&1
  client_check_status=$?
  cat "$COMPOSER_CLIENT_LOG_DIR/c8-3-types.log"
  printf 'native_exit=%s\n' "$client_check_status"
  test "$client_check_status" -eq 0
}
cd "$COMPOSER_WORKTREE" && git status --short
cd "$COMPOSER_WORKTREE" && git add -- src/elspeth/web/app.py src/elspeth/web/frontend/src/App.tsx src/elspeth/web/frontend/src/types/index.ts src/elspeth/web/frontend/src/types/composerOperations.ts src/elspeth/web/frontend/src/config/composer.ts src/elspeth/web/frontend/src/config/composer.test.ts src/elspeth/web/frontend/src/api/client.ts src/elspeth/web/frontend/src/stores/composerOperationCustody.ts src/elspeth/web/frontend/src/stores/composerOperationCustody.test.ts src/elspeth/web/frontend/src/stores/sessionStore.ts
cd "$COMPOSER_WORKTREE" && git diff --cached --name-only
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" scripts/branch-safety-check.sh --intent commit
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" git commit -m 'feat(composer): retain immutable lifecycle version in browser custody'
```

### Task C8.4: Keep one observer across segment expiry and successor starts

**Modify:** `api/composerOperationObserver.ts`; extend its three existing test
modules. This is a surgical change to its current auth-generation owner,
ambiguous-submission, active-attachment, Stop and terminal-settlement logic.

- [ ] Add imports for the v2 decoder, reducer and snapshot type. Add this
  exported optional callback to the existing `ObserverOptions` interface:

```typescript
continuation?: (snapshot: ComposerOperationSnapshotV2, successorStarted: boolean) => void;
```

- [ ] Immediately after `let observationDeadline = Number.POSITIVE_INFINITY`, create the reducer and
  this complete current-observer callback. `current()` is the existing
  principal/session/auth-generation predicate.

```typescript
const continuationState = new ComposerContinuationState();
let segmentTimerKey: string | null = null;
let segmentExpiry: ReturnType<typeof setTimeout> | undefined;
let expireCurrentStream: (() => void) | null = null;
const acceptContinuation = (snapshot: ComposerOperationSnapshotV2): boolean => {
  if (!current()) return false;
  const accepted = continuationState.accept(snapshot);
  if (!accepted.accepted) return false;
  options.continuation?.(snapshot, accepted.successorStarted);
  const segment = snapshot.segment;
  const nextKey = segment?.state === "running"
    ? `${snapshot.operation_id}:${segment.sequence}:${segment.generation}:${segment.deadline_at}` : null;
  if (nextKey !== segmentTimerKey) {
    if (segmentExpiry !== undefined) clearTimeout(segmentExpiry);
    segmentExpiry = undefined;
    segmentTimerKey = nextKey;
  }
  if (nextKey !== null && segmentExpiry === undefined && segment?.remaining_ms != null) {
    const expectedKey = nextKey;
    segmentExpiry = setTimeout(() => {
      if (!current() || segmentTimerKey !== expectedKey) return;
      // Close only this physical subscription. The logical owner and its
      // immutable submission/Stop custody continue to the next durable GET.
      expireCurrentStream?.();
    }, Math.max(0, segment.remaining_ms));
  }
  return true;
};
```

This first timer establishes an upper bound and never extends it for equal
revision refreshes. New sequence/generation changes clear/rearm it. An already
expired timer stays marked until identity changes; do not immediately rearm a
zero timer in a polling loop. Add `if (segmentExpiry !== undefined)
clearTimeout(segmentExpiry)` to the existing `finally`.

- [ ] At each API call forward `owner.descriptor.protocolVersion ?? 1`,
  including immediate Stop notifications and the captured Stop target. For
  captured Stop use `target.protocolVersion ?? 1`, not the later winner's
  version. Change ACK validation to this exact shape:

```typescript
if (ack.operation_id !== owner.descriptor.operationId || ack.kind !== owner.descriptor.kind) throw new Error("Composer acknowledgement identity mismatch");
if ("protocol_version" in ack) acceptContinuation(ack);
```

- [ ] In the Stop loop normalize status without changing terminal custody:

```typescript
if (cancelled.kind !== "session_missing" && cancelled.kind !== "operation_missing") {
  const status = "protocol_version" in cancelled ? cancelled.logical_status : cancelled.status;
  if ("protocol_version" in cancelled) acceptContinuation(cancelled);
  owner.stop = (status === "queued" || status === "running") && !cancelled.cancel_requested;
} else owner.stop = cancelled.kind === "operation_missing";
```

  Keep the existing preceding session-missing throw and kind check. The
  cancellation snapshot only stops retrying Stop; it never settles custody.
- [ ] Immediately after the GET missing/kind branches, normalize the public
  snapshot for the existing terminal logic using this exact code. Replace
  the old `snapshot.status` uses in that terminal block with `status`.

```typescript
const version2 = "protocol_version" in snapshot;
if (version2 && !acceptContinuation(snapshot)) {
  await sleep(Math.max(100, Math.min(snapshot.poll_after_ms, 60000)), owner.controller.signal);
  continue;
}
const status = version2 ? snapshot.logical_status : snapshot.status;
```

  Replace the final `tightenDeadline(snapshot.deadline_remaining_ms)` with
  `if (!version2) tightenDeadline(snapshot.deadline_remaining_ms)`.
  Existing v1 hard observation expiry remains unchanged.
- [ ] Replace the existing `readStream` with this complete function. Add
  `ComposerOperationDecodeError` to its existing decoder imports. Frame
  sequence is local to one subscription; the reducer owns durable ordering.

```typescript
async function readStream(
  owner: ObserverOwner,
  current: () => boolean,
  tightenDeadline: (remainingMs: number) => void,
  progress: ((snapshot: ComposerProgressSnapshot) => void) | undefined,
  acceptContinuation: (snapshot: ComposerOperationSnapshotV2) => boolean,
  continuationState: ComposerContinuationState,
  bindExpiry: (close: (() => void) | null) => void,
): Promise<void> {
  const controller = new AbortController();
  const close = () => controller.abort();
  bindExpiry(close);
  owner.controller.signal.addEventListener("abort", close, { once: true });
  let timer = setTimeout(close, 15000);
  const lifetimeTimer = setTimeout(close, 120000);
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  try {
    const response = await api.fetchComposerOperationStream(
      owner.descriptor.sessionId, owner.descriptor.operationId, controller.signal,
      owner.descriptor.protocolVersion ?? 1,
    );
    if (!current() || !response.ok || response.headers.get("content-type")?.split(";")[0] !== "text/event-stream" || response.body === null) return;
    const selected = response.headers.get("Composer-Operation-Version");
    if (selected !== null && selected !== "1" && selected !== "2") throw new ComposerOperationDecodeError("Unsupported stream version");
    const version2 = selected === "2";
    if (version2 && owner.descriptor.protocolVersion !== 2) throw new ComposerOperationDecodeError("Unsupported selected stream version");
    reader = response.body.getReader();
    const decoder = new ComposerSseDecoder();
    let sequence = -1;
    while (current()) {
      const chunk = await reader.read();
      if (!current()) return;
      if (chunk.done) { decoder.finish(); return; }
      for (const value of decoder.push(chunk.value)) {
        if (version2) {
          const frame = decodeComposerOperationFrameV2(value, owner.descriptor.sessionId, owner.descriptor.operationId);
          if (frame.sequence <= sequence) continue;
          sequence = frame.sequence;
          clearTimeout(timer); timer = setTimeout(close, 15000);
          if (frame.event === "status" || frame.event === "terminal") {
            acceptContinuation(frame.payload);
            if (frame.event === "terminal") return;
          } else if (frame.event === "progress" && continuationState.acceptsProgress(
            frame.status_revision, frame.payload.segment_sequence, frame.payload.segment_generation,
          )) {
            progress?.({
              session_id: frame.session_id, inflight_requests: 1,
              request_id: frame.payload.request_id,
              phase: frame.payload.phase, headline: frame.payload.headline,
              evidence: frame.payload.evidence, likely_next: frame.payload.likely_next,
              reason: frame.payload.reason, updated_at: frame.payload.updated_at,
            });
          }
        } else {
          const frame = decodeComposerOperationFrame(value, owner.descriptor.sessionId, owner.descriptor.operationId);
          if (frame.sequence <= sequence) continue;
          sequence = frame.sequence;
          clearTimeout(timer); timer = setTimeout(close, 15000);
          if (frame.event === "status") tightenDeadline(frame.payload.deadline_remaining_ms);
          if (frame.event === "terminal") return;
          if (frame.event === "progress") progress?.({ ...frame.payload, session_id: frame.session_id, inflight_requests: 1 });
        }
      }
    }
  } catch (error) {
    if (error instanceof ComposerOperationDecodeError) throw error;
    // The existing durable GET observer handles physical transport fallback.
  } finally {
    clearTimeout(timer); clearTimeout(lifetimeTimer); controller.abort();
    bindExpiry(null);
    owner.controller.signal.removeEventListener("abort", close);
    if (reader !== undefined) { await reader.cancel().catch(() => undefined); reader.releaseLock(); }
  }
}
```

- [ ] Replace the owning observer's old `readStream` call with this exact call.
  Add `error instanceof ComposerOperationDecodeError` to the existing GET
  catch's immediate-rethrow condition, before the transport retry sleep.
  Equal-revision semantic disagreement now surfaces as a protocol error while
  retained submission/Stop custody remains available for reconciliation.

```typescript
await readStream(owner, current, tightenDeadline, options.progress,
  acceptContinuation, continuationState, (close) => { expireCurrentStream = close; });
```

- [ ] Add a deterministic fake-timer observer regression using the existing
  client mocks and authenticated scope fixture. This complete test is added
  to `composerOperationObserver.deadlines.test.ts`, whose existing
  `descriptor()`, `beforeEach`, and `afterEach` definitions remain unchanged.

```typescript
it("v2 expired segment reconnects to the same parent without cancelling or resubmitting", async () => {
  const operation = { ...descriptor(), protocolVersion: 2 as const };
  const live = { protocol_version: 2 as const, operation_id: operationId,
    kind: "compose_message" as const, logical_status: "running" as const,
    phase: "executing" as const, status_revision: 1, poll_after_ms: 1000,
    cancel_requested: false, result: null, error: null,
    segment: { sequence: 1, generation: 1, state: "running" as const,
      observed_at: "2026-10-20T00:00:00Z", deadline_at: "2026-10-20T00:00:01Z",
      remaining_ms: 1000, rollover_pending: false } };
  vi.mocked(api.submitComposerOperation).mockResolvedValue(live);
  let physicalSignal: AbortSignal | null = null;
  vi.mocked(api.fetchComposerOperationStream).mockImplementation(async (_sid, _id, signal) => {
    physicalSignal = signal;
    const body = new ReadableStream<Uint8Array>({ start(controller) {
      signal.addEventListener("abort", () => controller.error(new DOMException("aborted", "AbortError")), { once: true });
    } });
    return new Response(body, { headers: { "Content-Type": "text/event-stream", "Composer-Operation-Version": "2" } });
  });
  vi.mocked(api.fetchComposerOperation).mockResolvedValue({
    ...live, logical_status: "completed", phase: "settled", status_revision: 3,
    segment: null, error: null, result: { state: null, proposals: [], message: {
      id: operationId, operation_id: operationId, session_id: sessionId, role: "assistant",
      content: "done", raw_content: null, segments: [{ kind: "text", content: "done" }],
      tool_calls: null, created_at: "2026-10-20T00:00:02Z", composition_state_id: null,
      tool_call_id: null, parent_assistant_id: null, sequence_no: 1, rejection: null,
    } },
  });
  const observing = submitAndObserveComposerOperation(operation);
  await vi.advanceTimersByTimeAsync(999);
  expect(api.fetchComposerOperation).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(1);
  expect(physicalSignal).not.toBeNull();
  expect((await observing).message.content).toBe("done");
  expect(api.fetchComposerOperation).toHaveBeenCalledWith(sessionId, operationId, 2);
  expect(api.cancelComposerOperation).not.toHaveBeenCalled();
  expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
});
```

- [ ] Run RED before implementation and GREEN afterward for all three
  observer files, both decoders, custody/store tests and frontend typecheck.
  Add explicit equal-revision conflict, auth-switch, stale-generation and
  successor queued-to-running variants using the new reducer's test factory.
  Commit with `feat(composer): preserve observation across segment rollover`.

```bash
cd "$COMPOSER_WORKTREE" && {
  npm --prefix "$PWD/src/elspeth/web/frontend" test -- src/api/composerOperationObserver.test.ts src/api/composerOperationObserver.deadlines.test.ts src/api/composerOperationObserver.recovery.test.ts src/api/composerOperationDecoder.test.ts src/api/composerOperationV2Decoder.test.ts src/stores/composerOperationCustody.test.ts src/stores/sessionStore.composerOperations.test.ts > "$COMPOSER_CLIENT_LOG_DIR/c8-4-ui.log" 2>&1
  client_check_status=$?
  cat "$COMPOSER_CLIENT_LOG_DIR/c8-4-ui.log"
  printf 'native_exit=%s\n' "$client_check_status"
  test "$client_check_status" -eq 0
}
cd "$COMPOSER_WORKTREE" && {
  npm --prefix "$PWD/src/elspeth/web/frontend" run typecheck > "$COMPOSER_CLIENT_LOG_DIR/c8-4-types.log" 2>&1
  client_check_status=$?
  cat "$COMPOSER_CLIENT_LOG_DIR/c8-4-types.log"
  printf 'native_exit=%s\n' "$client_check_status"
  test "$client_check_status" -eq 0
}
cd "$COMPOSER_WORKTREE" && git status --short
cd "$COMPOSER_WORKTREE" && git add -- src/elspeth/web/frontend/src/api/composerOperationObserver.ts src/elspeth/web/frontend/src/api/composerOperationObserver.test.ts src/elspeth/web/frontend/src/api/composerOperationObserver.deadlines.test.ts src/elspeth/web/frontend/src/api/composerOperationObserver.recovery.test.ts
cd "$COMPOSER_WORKTREE" && git diff --cached --name-only
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" scripts/branch-safety-check.sh --intent commit
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" git commit -m 'feat(composer): preserve observation across segment rollover'
```

### Task C8.5: Quiet five-second activity notice, actual phase and generated types

**Modify:** `stores/sessionStore.ts`, `hooks/useComposer.ts`,
`components/chat/ChatPanel.tsx`, `components/chat/ComposingIndicator.tsx`.
**Extend:** `components/chat/ComposingIndicator.test.tsx` and store tests.

- [ ] Add these exact types near the existing store interface and these fields
  to both its interface and `initialState` (the initializer values follow).

```typescript
type ContinuationNotice = Readonly<{ key: string; expiresAt: number }>;

// SessionState interface additions
composerContinuation: import("@/types/composerOperations").ComposerOperationSnapshotV2 | null;
composerContinuationNotice: ContinuationNotice | null;

// initialState additions
composerContinuation: null as import("@/types/composerOperations").ComposerOperationSnapshotV2 | null,
composerContinuationNotice: null as ContinuationNotice | null,
```

- [ ] In `runDurableComposerTurn`'s existing options object add this callback.
  Its session/publication/auth-generation guard is the same predicate used
  by existing progress. A reconnect's baseline snapshot never requests a
  notice, and neither callback creates a transcript message.

```typescript
continuation: (snapshot: import("@/types/composerOperations").ComposerOperationSnapshotV2, successorStarted: boolean) => {
  if (!current() || reconciliationOnly) return;
  const terminal = snapshot.logical_status === "completed" || snapshot.logical_status === "failed";
  const stopped = snapshot.cancel_requested;
  const segment = snapshot.segment;
  const prior = useSessionStore.getState().composerContinuationNotice;
  useSessionStore.setState({
    composerContinuation: snapshot,
    composerContinuationNotice: terminal || stopped ? null : successorStarted && segment !== null
      ? { key: `${snapshot.operation_id}:${segment.sequence}`, expiresAt: Date.now() + 5000 }
      : prior,
  });
},
```

- [ ] In the current direct human `cancelComposition` handler, clear
  `composerContinuationNotice` before dispatching cancellation, so the notice
  disappears even while the Stop persistence request is in flight. Also clear
  both new fields in every existing session-change/unbind/reset state update
  that currently clears `composerProgress`; this is owned session state,
  never a global notice carried into another session. Starting a new logical
  compose/recompose clears them alongside `composerProgress`. At the first
  line of `cancelComposition` insert:

```typescript
set({ composerContinuationNotice: null });
```

  In the reset objects of `createSession`, `archiveSession`'s active-session
  branch, both `selectSession` reset/failure branches,
  `resetForTutorialSession`, `unbindMissingSession`, and both
  `forkFromMessage` success/failure branches, replace the existing reset
  property with this exact property group:

```typescript
composerProgress: null,
composerContinuation: null,
composerContinuationNotice: null,
```

  In both `sendMessage` and `retryMessage`'s `set((current) => ({ ... }))`
  objects add `composerContinuation: null, composerContinuationNotice: null`
  immediately beside `composerProgress: null`. `reset()` already spreads
  `initialState`, so the new initial fields clear there without another
  reset implementation. `startComposerProgressPolling` keeps its existing
  progress-only clearing; restarting that transport is not a new logical
  request and must not reset the continuation notice watermark.
- [ ] In `useComposer` return the new fields using these exact selectors:

```typescript
const composerContinuation = useSessionStore((s) => s.composerContinuation);
const composerContinuationNotice = useSessionStore((s) => s.composerContinuationNotice);
```

  Add `composerContinuation, composerContinuationNotice` to its return
  object. In `ChatPanel`, the existing `useComposer` destructure receives
  both and forwards them to its existing `ComposingIndicator` instance:

```typescript
const {
  sendMessage, retryMessage, cancelComposition, isComposing, error, errorDetails,
  composerContinuation, composerContinuationNotice,
} = composer;
```

```tsx
<ComposingIndicator
  latestRequest={activeComposerMessage?.content ?? null}
  compositionState={compositionState}
  composerProgress={composerProgress}
  composerContinuation={composerContinuation}
  composerContinuationNotice={composerContinuationNotice}
  completionOutcome={freeformCompletionOutcome}
  liveToolCalls={liveToolCalls}
  lastRejection={lastComposerRejection}
  onRetryInterruptedRequest={interruptedRequest === undefined
    ? undefined : () => { void retryMessage(interruptedRequest.id); }}
  retryDisabledReason={composeTimeoutReady ? null : decisionApplyDisabledReason}
/>
```
- [ ] Add these optional props to `ComposingIndicatorProps` and default both
  to null in its function destructure:

```typescript
composerContinuation?: import("@/types/composerOperations").ComposerOperationSnapshotV2 | null;
composerContinuationNotice?: Readonly<{ key: string; expiresAt: number }> | null;
```

- [ ] After `detailsOpen` state, add this complete local expiry effect. It
  clears locally on the first permitted event-loop turn after throttling;
  it does not depend on another server event.

```typescript
const [visibleContinuationKey, setVisibleContinuationKey] = useState<string | null>(null);
useEffect(() => {
  const notice = composerContinuationNotice;
  const terminal = composerContinuation?.logical_status === "completed" || composerContinuation?.logical_status === "failed";
  if (notice === null || terminal || composerContinuation?.cancel_requested) {
    setVisibleContinuationKey(null);
    return;
  }
  const remaining = notice.expiresAt - Date.now();
  if (remaining <= 0) { setVisibleContinuationKey(null); return; }
  setVisibleContinuationKey(notice.key);
  const timer = setTimeout(() => setVisibleContinuationKey((key) => key === notice.key ? null : key), remaining);
  return () => clearTimeout(timer);
}, [composerContinuationNotice, composerContinuation?.logical_status, composerContinuation?.cancel_requested]);
const continuationHeadline = composerContinuation?.phase === "continuation_queued" ? "Waiting to continue"
  : composerContinuation?.phase === "reconciling" ? "Checking the previous action"
  : composerContinuation?.phase === "retry_waiting" ? "Waiting to retry"
  : composerContinuation?.phase === "queued" ? "Waiting to start"
  : null;
```

- [ ] Inside the existing `role="status"` summary insert this exact visible
  notice. Replace the title's `{workingView.headline}` with
  `{continuationHeadline ?? workingView.headline}`. All backend working
  evidence and current tool action lists remain unchanged.

```tsx
{visibleContinuationKey !== null && visibleContinuationKey === composerContinuationNotice?.key && (
  <div className="composing-text">I'm still working on this</div>
)}
```

- [ ] Append this complete UI test using existing file imports `render`,
  `screen`, `act`, `vi`, `expect`, `it`, and `ComposingIndicator`.

```tsx
it("clears a successor notice locally after five seconds and on Stop", async () => {
  vi.useFakeTimers();
  try {
    const continuation = { protocol_version: 2 as const,
      operation_id: "22222222-2222-4222-8222-222222222222", kind: "compose_message" as const,
      logical_status: "running" as const, phase: "executing" as const,
      status_revision: 4, poll_after_ms: 1000, cancel_requested: false, result: null, error: null,
      segment: { sequence: 2, generation: 1, state: "running" as const,
        observed_at: "2026-10-20T00:00:00Z", deadline_at: "2026-10-20T00:05:00Z",
        remaining_ms: 300000, rollover_pending: false } };
    const notice = { key: `${continuation.operation_id}:2`, expiresAt: Date.now() + 5000 };
    const { rerender } = render(<ComposingIndicator composerContinuation={continuation} composerContinuationNotice={notice} />);
    expect(screen.getByText("I'm still working on this")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(4999); });
    expect(screen.getByText("I'm still working on this")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(screen.queryByText("I'm still working on this")).not.toBeInTheDocument();
    const next = { key: `${continuation.operation_id}:3`, expiresAt: Date.now() + 5000 };
    rerender(<ComposingIndicator composerContinuation={continuation} composerContinuationNotice={next} />);
    expect(screen.getByText("I'm still working on this")).toBeInTheDocument();
    rerender(<ComposingIndicator composerContinuation={{ ...continuation, cancel_requested: true }} composerContinuationNotice={next} />);
    expect(screen.queryByText("I'm still working on this")).not.toBeInTheDocument();
  } finally { vi.useRealTimers(); }
});
```

- [ ] Run all named observer/decoder/custody/store/indicator tests and
  frontend typecheck/lint. Require native 0. Candidate source uses hand-written
  `src/elspeth/web/frontend/src/types/api.ts` reexports; the generator output
  `src/elspeth/web/frontend/src/types/api.generated.ts` is not a tracked or
  consumed existing module. Keep the canonical manual types updated above.
  If an owned authorized localhost:8000 service is later available, the
  existing `npm --prefix "$PWD/src/elspeth/web/frontend" run generate-types`
  can compare OpenAPI projection against those types. Its output is a newly
  generated comparison artifact, not an additional implementation dependency
  or a reason to start/query an unrelated service. Do not stage an unused
  generated file as part of the commit below.
- [ ] Commit with `feat(composer): show quiet continuation activity without transcript noise`.

```bash
cd "$COMPOSER_WORKTREE" && {
  npm --prefix "$PWD/src/elspeth/web/frontend" test -- src/stores/sessionStore.composerOperations.test.ts src/components/chat/ComposingIndicator.test.tsx > "$COMPOSER_CLIENT_LOG_DIR/c8-5-ui.log" 2>&1
  client_check_status=$?
  cat "$COMPOSER_CLIENT_LOG_DIR/c8-5-ui.log"
  printf 'native_exit=%s\n' "$client_check_status"
  test "$client_check_status" -eq 0
}
cd "$COMPOSER_WORKTREE" && {
  npm --prefix "$PWD/src/elspeth/web/frontend" run typecheck > "$COMPOSER_CLIENT_LOG_DIR/c8-5-types.log" 2>&1
  client_check_status=$?
  cat "$COMPOSER_CLIENT_LOG_DIR/c8-5-types.log"
  printf 'native_exit=%s\n' "$client_check_status"
  test "$client_check_status" -eq 0
}
cd "$COMPOSER_WORKTREE" && git status --short
cd "$COMPOSER_WORKTREE" && git add -- src/elspeth/web/frontend/src/stores/sessionStore.ts src/elspeth/web/frontend/src/hooks/useComposer.ts src/elspeth/web/frontend/src/components/chat/ChatPanel.tsx src/elspeth/web/frontend/src/components/chat/ComposingIndicator.tsx src/elspeth/web/frontend/src/components/chat/ComposingIndicator.test.tsx
cd "$COMPOSER_WORKTREE" && git diff --cached --name-only
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" scripts/branch-safety-check.sh --intent commit
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" git commit -m 'feat(composer): show quiet continuation activity without transcript noise'
```

### Task S7.1: Preserve the already represented selected .7 fixes

**Existing production and test inputs:**

| Exact path | Required preserved behavior |
| --- | --- |
| `src/elspeth/web/auth/middleware.py` | A real rejected authentication request has the exact bare Bearer challenge. |
| `src/elspeth/web/composer/tools/sources.py` | Compare complete reconciled owned content with strict JSON; a semantic no-op preserves the exact original state and progress. |
| `src/elspeth/web/frontend/src/api/shareableReviews.ts` | Classify the exact challenge as authentication; capability refusal retains authenticated session custody. |
| `src/elspeth/web/frontend/src/api/shareableReviews.test.ts` | Exact challenge classes and delayed refusal across replacement auth generations. |
| `src/elspeth/web/frontend/src/components/shared/SharedInspectView.tsx` | Recover sign-in without declaring a valid capability link invalid. |
| `src/elspeth/web/frontend/src/components/shared/SharedInspectView.test.tsx` | Render authentication and capability errors separately. |
| `tests/integration/web/test_shareable_reviews_routes.py` | Auth challenge precedes snapshot resolution; invalid capability remains a capability refusal. |
| `tests/unit/web/auth/test_middleware.py` | Header classification on authenticated middleware failures. |
| `tests/unit/web/auth/test_routes.py` | Preserve shared authentication integration behavior. |
| `tests/unit/web/composer/test_patch_source_options_noop.py` | Handler and dispatch preserve semantic no-op, review evidence and numeric type distinctions. |
| `tests/unit/web/composer/test_source_rejection_contracts.py` | Empty/no-op patches still enforce safety and source contracts. |

The selected source is commit `0d216f712fc9bc4452856f9a774a6ef2c598b052`.
All paths above are already blob-identical in `ce410353`; this task preserves
and reconciles those fixes after Continuation instead of replaying the commit
or importing a staged owner index. Historical staged SMK10 is no longer an
execution prerequisite. Do not restore its old serialization fallback.

- [ ] Re-read the exact selected commit and compare these paths against the
  P0-admitted combined checkout. A blank diff and native 0 mean no replay is
  needed; if an earlier Continuation task touched one of these paths, inspect
  that current delta and retain the concrete semantics below.

```bash
cd "$COMPOSER_WORKTREE" && git show --stat --oneline 0d216f712fc9bc4452856f9a774a6ef2c598b052
cd "$COMPOSER_WORKTREE" && git diff 0d216f712fc9bc4452856f9a774a6ef2c598b052 HEAD -- src/elspeth/web/auth/middleware.py src/elspeth/web/composer/tools/sources.py src/elspeth/web/frontend/src/api/shareableReviews.ts src/elspeth/web/frontend/src/api/shareableReviews.test.ts src/elspeth/web/frontend/src/components/shared/SharedInspectView.tsx src/elspeth/web/frontend/src/components/shared/SharedInspectView.test.tsx tests/integration/web/test_shareable_reviews_routes.py tests/unit/web/auth/test_middleware.py tests/unit/web/auth/test_routes.py tests/unit/web/composer/test_patch_source_options_noop.py tests/unit/web/composer/test_source_rejection_contracts.py
```

- [ ] Check the current source-option handler still performs authoritative
  review reconciliation before this exact existing decision. These are
  preservation criteria, not instructions to overwrite the whole handler.

```python
previous_content = project_composer_authority_payload(state.to_dict())
reconciled_content = project_composer_authority_payload(new_state.to_dict())
reconciled_content["version"] = state.version
unchanged = json.dumps(previous_content, sort_keys=True, allow_nan=False) == json.dumps(
    reconciled_content, sort_keys=True, allow_nan=False
)
if unchanged:
    return _mutation_result(state, (), data=data)
return _mutation_result(new_state, (_source_component_id(source_name),), data=data)
```

`project_composer_authority_payload` is the existing function in
`src/elspeth/web/composer/authority_hashing.py`; `_mutation_result` and
`_source_component_id` are existing imports/helpers in `sources.py`. Do not
catch owned-state `TypeError`/`ValueError` from this JSON comparison. Preserve
bool/int/float distinctions and review-only changes; a no-op cannot bypass
credential, source-path, plugin option or authoritative review admission.

- [ ] Check the current shared-view client still uses this exact existing
  classification around its production parser, preserving auth generation
  checks in `authSession.ts` and `client.ts`.

```typescript
const authenticationRefused = response.status === 401 &&
  response.headers.get("WWW-Authenticate")?.trim().toLowerCase() === "bearer";
let body: unknown;
try {
  body = await parseResponse<unknown>(response, { logoutOnUnauthorized: authenticationRefused });
} catch (error) {
  if (authenticationRefused && isRecord(error) && error.status === 401 && typeof error.detail === "string") {
    throw { ...error, error_type: "authentication_required" };
  }
  throw error;
}
return validateSharedInspectResponse(body, response.status);
```

`parseResponse`, `isRecord` and `validateSharedInspectResponse` are existing
definitions/imports in `src/elspeth/web/frontend/src/api/shareableReviews.ts`.
The UI's corresponding `_classifyError` keeps its existing
`authentication_required` branch. Generic or parameterized challenges do not
acquire this classification.

- [ ] Run the exact existing behavioral packet on the combined tree after
  C7/C8. This preserved source requires no new artificial feature RED. The
  suite already contains the counterexamples: missing/unrecognized challenge,
  delayed refusal against replacement auth, int/bool/float substitutions,
  owned opaque object/NaN propagation, review rehydration, missing source and
  invalid source path/options. Require native 0 and inspect the complete logs.

```bash
cd "$COMPOSER_WORKTREE" && {
  PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" "$COMPOSER_PYTHON" -m pytest tests/unit/web/auth/test_middleware.py tests/unit/web/auth/test_routes.py tests/integration/web/test_shareable_reviews_routes.py tests/unit/web/composer/test_patch_source_options_noop.py tests/unit/web/composer/test_source_rejection_contracts.py -n 0 -q > "$COMPOSER_CLIENT_LOG_DIR/s7-backend.log" 2>&1
  client_check_status=$?
  cat "$COMPOSER_CLIENT_LOG_DIR/s7-backend.log"
  printf 'native_exit=%s\n' "$client_check_status"
  test "$client_check_status" -eq 0
}
cd "$COMPOSER_WORKTREE" && {
  npm --prefix "$PWD/src/elspeth/web/frontend" test -- src/api/shareableReviews.test.ts src/components/shared/SharedInspectView.test.tsx src/hooks/useSharedRoute.test.ts > "$COMPOSER_CLIENT_LOG_DIR/s7-ui.log" 2>&1
  client_check_status=$?
  cat "$COMPOSER_CLIENT_LOG_DIR/s7-ui.log"
  printf 'native_exit=%s\n' "$client_check_status"
  test "$client_check_status" -eq 0
}
```

- [ ] If no source changes were required, record preservation in the ordinary
  integration PR description; do not create an empty ceremony commit. Any
  necessary conflict resolution is reviewed on its actual diff, tested with
  the packet above, and committed through normal hooks with its own exact
  owned pathspecs. Green historical results are not reused for the new tree.

### Task S7.2: Integrate the selected net Advisor repair

**Reference:** compare donor parent
`2d5c0557e2d3dfcf6f176b94e9d17290d3da7a31` to selected final tree
`e6b247943f911637f144fd11ba1c8130397fa2a9` (tree
`27a663ca955011e3af752f001d56c3ce537a208f`). The final commit's parent is
`b09f5419764b69a1761dbce856f32fa96fde20a6`; its tip delta alone removes URL
retention and repairs a fixture. The selected repair therefore comes from the
net comparison, never a cherry-pick of that tip or the entire parent branch.
The exact selected paths are:

```text
src/elspeth/web/composer/progress.py
src/elspeth/web/interpretation_state.py
src/elspeth/web/sessions/pending_interpretation.py
tests/unit/web/composer/test_compose_loop_interpretation_review_dispatch.py
tests/unit/web/composer/test_progress.py
tests/unit/web/sessions/test_interpretation_events_service.py
tests/unit/web/test_interpretation_state.py
```

- [ ] Read this exact net patch and compare each semantic hunk with the actual
  .9 GREEN integration base. The source read on 21 October measured 47,088
  bytes and SHA-256 `8af35dea091d2057c23ce1999fa2261252c01d62b07f1001ab22ec7100d5df9e`;
  the owner-retained `selected-net-seven.patch` matched those exact Git bytes.
  These identify the reviewed input, not a plan admission manifest.

```bash
cd "$COMPOSER_WORKTREE" && git diff 2d5c0557e2d3dfcf6f176b94e9d17290d3da7a31 e6b247943f911637f144fd11ba1c8130397fa2a9 -- src/elspeth/web/composer/progress.py src/elspeth/web/interpretation_state.py src/elspeth/web/sessions/pending_interpretation.py tests/unit/web/composer/test_compose_loop_interpretation_review_dispatch.py tests/unit/web/composer/test_progress.py tests/unit/web/sessions/test_interpretation_events_service.py tests/unit/web/test_interpretation_state.py
```

- [ ] Curate only missing semantic hunks: neutral provider progress, raw-field
  provenance through actual upstream graph routes and source-side mapper
  projections, and prompt-placeholder substitution bound to reviewed values
  without English label matching. Keep the new .9 rejection and guard fixes,
  current evidence/continuation/Stop owners, and the URL-retention exclusion.
  Review every resulting file in full; do not replay whole files.
- [ ] Run the four selected affected test files through ordinary pytest with
  retained log and native exit, then the combined Q gate. If the admitted base
  already contains a selected hunk, prove it from the actual source and omit
  the duplicate. Commit only the resulting selected repair and tests through
  normal hooks; record a no-op in the ordinary PR if all are represented.

Source comparison is not integration or runtime qualification.

## Exit and dependency boundaries

These are the exact final affected packets; they run only after P0 and the
task-specific RED/GREEN steps. Their results are scoped to the affected
transport and browser behavior. The main integration checkpoint owns the
required broader gates and actual shared test capacity.

```bash
cd "$COMPOSER_WORKTREE" && {
  PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" "$COMPOSER_PYTHON" -m pytest tests/unit/web/sessions/test_composer_continuation_dtos.py tests/unit/web/sessions/test_composer_operation_versions.py tests/unit/web/sessions/test_composer_operation_dtos.py tests/unit/web/sessions/test_composer_operation_error_parity.py tests/unit/web/sessions/routes/composer/test_operation_outcomes.py tests/unit/web/sessions/test_operation_receipts.py tests/unit/web/test_composer_stream.py tests/unit/web/test_composer_stream_worker_custody.py -n 0 -q > "$COMPOSER_CLIENT_LOG_DIR/client-backend-final.log" 2>&1
  client_check_status=$?
  cat "$COMPOSER_CLIENT_LOG_DIR/client-backend-final.log"
  printf 'native_exit=%s\n' "$client_check_status"
  test "$client_check_status" -eq 0
}
cd "$COMPOSER_WORKTREE" && {
  npm --prefix "$PWD/src/elspeth/web/frontend" test -- src/config/composer.test.ts src/api/client.composerTransport.test.ts src/api/composerOperationDecoder.test.ts src/api/composerOperationV2Decoder.test.ts src/api/composerContinuationState.test.ts src/api/composerOperationObserver.test.ts src/api/composerOperationObserver.deadlines.test.ts src/api/composerOperationObserver.recovery.test.ts src/stores/composerOperationCustody.test.ts src/stores/sessionStore.composerOperations.test.ts src/components/chat/ComposingIndicator.test.tsx > "$COMPOSER_CLIENT_LOG_DIR/client-ui-final.log" 2>&1
  client_check_status=$?
  cat "$COMPOSER_CLIENT_LOG_DIR/client-ui-final.log"
  printf 'native_exit=%s\n' "$client_check_status"
  test "$client_check_status" -eq 0
}
cd "$COMPOSER_WORKTREE" && {
  npm --prefix "$PWD/src/elspeth/web/frontend" run typecheck > "$COMPOSER_CLIENT_LOG_DIR/client-types-final.log" 2>&1
  client_check_status=$?
  cat "$COMPOSER_CLIENT_LOG_DIR/client-types-final.log"
  printf 'native_exit=%s\n' "$client_check_status"
  test "$client_check_status" -eq 0
}
cd "$COMPOSER_WORKTREE" && {
  npm --prefix "$PWD/src/elspeth/web/frontend" run lint > "$COMPOSER_CLIENT_LOG_DIR/client-lint-final.log" 2>&1
  client_check_status=$?
  cat "$COMPOSER_CLIENT_LOG_DIR/client-lint-final.log"
  printf 'native_exit=%s\n' "$client_check_status"
  test "$client_check_status" -eq 0
}
```

Before each Python-bearing commit, run focused Ruff/format over the task's
exact Python pathspecs and every relevant whole-tree gate listed in
`CONTRIBUTING.md`. Shared schema/SQL changes remain subject to the storage
companion's serial PostgreSQL packet; frontend checks never substitute for it.

- [ ] C7 returns exact supported/refused outcomes for every v1/v2 combination,
  persists human Stop before its old-client ACK, and exposes no checkpoint or
  private prompt/tool/native bytes through status/error/stream projection.
- [ ] C8 retains original submission and Stop custody across reconnect and
  segment expiry, rejects durable revision disagreement, suppresses historical
  notices and clears each visible successor notice locally after five seconds.
- [ ] S7's selected fixes remain represented on the actual combined source;
  the separately scoped b09 carry is explicitly accounted for by its owner.
- [ ] The storage companion's real PostgreSQL Stop/yield/claim/final/Revert
  interleavings and C4b physical-accounting recovery pass before Q. The SQLite
  HTTP tests here do not substitute for those contention proofs.
- [ ] Keep `composer_continuation_enabled=False` through these tasks and all
  prerequisites. Run the main Q acceptance and operator rollout decision
  before enabling new admissions; existing v2 stores still require the
  selected preserve/refuse deployment path.

Planning handoff: save this companion with the main and storage plans, then
stand by. No implementation or execution option is selected by this document.
