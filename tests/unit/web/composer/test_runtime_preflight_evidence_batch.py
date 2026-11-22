"""The runtime-preflight reuse evidence read is ONE fenced transaction at any N.

Real SQLite sessions engine, real session-operation authority, real
``BlobServiceImpl``, real ``WebSecretService`` and a real ``ComposerPreflight``
(no doubles). Statements and transactions are counted at the SQLAlchemy engine
(``before_cursor_execute`` and ``begin``); the counter is controlled first
against known counts. A fence check is the authority's exact-context
compare-and-swap, ``UPDATE session_operation_fences``: one per fenced
transaction, the only write a read makes.

Before this change the evidence read was one fenced transaction per record
(N + 1 transactions and N fence writes per reuse hit); now it is the fenced
batch plus the secret inventory read (2 transactions, 1 fence write).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import structlog
from sqlalchemy import Engine, event, text

from elspeth.contracts.blobs import BlobNotFoundError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.catalog.validation_reuse import MutationEpoch
from elspeth.web.composer import composer_preflight as preflight_module
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    model_choice_artifact_hash,
    prompt_review_anchor_hash_from_options,
)
from elspeth.web.secrets.server_store import ServerSecretStore
from elspeth.web.secrets.service import ScopedSecretResolver, WebSecretService
from elspeth.web.secrets.user_store import UserSecretStore
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.composer._helpers import _make_settings

_FENCE_CHECK = "UPDATE session_operation_fences"
_BLOB_ROW_READ = "FROM blobs WHERE blobs.id ="


@dataclass
class _Phase:
    transactions: int = 0
    statements: int = 0
    fence_checks: int = 0
    writes: int = 0
    blob_row_ids: list[str] = field(default_factory=list)


class _Counter:
    """Counts what the engine runs, per named phase; nothing outside a phase."""

    def __init__(self, engine: Engine) -> None:
        self._current: _Phase | None = None
        event.listen(engine, "before_cursor_execute", self._on_statement)
        event.listen(engine, "begin", self._on_begin)

    def _on_statement(self, _conn: Any, _cursor: Any, statement: str, parameters: Any, _context: Any, _many: bool) -> None:
        phase = self._current
        if phase is None:
            return
        flat = " ".join(statement.split())
        # The SQLite session engine issues its own BEGIN per transaction
        # (sessions/engine.py); the ``begin`` event already counts those.
        if flat.upper().startswith("BEGIN"):
            return
        phase.statements += 1
        if flat.startswith(_FENCE_CHECK):
            phase.fence_checks += 1
        if flat.startswith(("UPDATE", "INSERT", "DELETE")):
            phase.writes += 1
        if _BLOB_ROW_READ in flat:
            # Bound in the statement's order: the blob id first, then the custody session id.
            phase.blob_row_ids.append(parameters[0])

    def _on_begin(self, _conn: Any) -> None:
        if self._current is not None:
            self._current.transactions += 1

    @contextmanager
    def phase(self) -> Iterator[_Phase]:
        counted = _Phase()
        self._current = counted
        try:
            yield counted
        finally:
            self._current = None


@dataclass
class _Rig:
    engine: Engine
    counter: _Counter
    sessions: SessionServiceImpl
    blobs: BlobServiceImpl
    service: ComposerServiceImpl
    context: SessionOperationContext
    owner: str
    session_id: UUID
    data_dir: Path
    blob_ids: list[UUID]

    def state(self, extra_markers: tuple[dict[str, str], ...] = ()) -> CompositionState:
        markers = [self.marker(blob_id) for blob_id in self.blob_ids] + list(extra_markers)
        queries = {
            f"q{index:02d}": {"input_fields": {"f1": "text"}, "response_format": "standard", "template": marker}
            for index, marker in enumerate(markers)
        }
        options: dict[str, Any] = {
            "provider": "openrouter",
            "model": "openai/gpt-4o-mini",
            "queries": queries,
            "required_input_fields": [],
            "max_capacity_retry_seconds": 30,
            "schema": {"mode": "observed"},
        }
        # Both reviews resolved, so a compute reaches materialization and its own blob reads.
        anchor = prompt_review_anchor_hash_from_options(options)
        assert anchor is not None
        options[INTERPRETATION_REQUIREMENTS_KEY] = [
            {
                "id": "llm_prompt_template:judge",
                "kind": "llm_prompt_template",
                "user_term": "llm_prompt_template:judge",
                "status": "resolved",
                "draft": "surface",
                "event_id": "evt-judge",
                "accepted_value": "surface",
                "accepted_artifact_hash": None,
                "resolved_prompt_template_hash": anchor,
            },
            {
                "id": "model_choice_review:judge",
                "kind": "llm_model_choice",
                "user_term": "llm_model_choice:judge",
                "status": "resolved",
                "draft": "openai/gpt-4o-mini",
                "event_id": "evt-model",
                "accepted_value": "openai/gpt-4o-mini",
                "accepted_artifact_hash": None,
                "resolved_prompt_template_hash": model_choice_artifact_hash("openai/gpt-4o-mini"),
            },
        ]
        source_path = self.data_dir / "blobs" / str(self.session_id) / "input.csv"
        return CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="rows",
                options={"path": str(source_path), "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            ),
            nodes=(
                NodeSpec(
                    id="judge",
                    node_type="transform",
                    plugin="llm",
                    input="rows",
                    on_success="results",
                    on_error="discard",
                    options=options,
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            edges=(),
            outputs=(
                OutputSpec(
                    name="results",
                    plugin="json",
                    options={"path": "outputs/results.jsonl", "format": "jsonl", "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(name="evidence-batch"),
            version=1,
        )

    def marker(self, blob_id: UUID) -> dict[str, str]:
        record = asyncio.run(self.blobs.get_blob(blob_id, session_operation_context=self.context))
        assert record.content_hash is not None
        return {"blob_ref": str(record.id), "mode": "inline_content", "sha256": record.content_hash}

    def new_session_blob(self, filename: str) -> tuple[UUID, SessionOperationContext]:
        """A blob in another session of the same owner, created under that session's own fence."""
        other = asyncio.run(self.sessions.create_session(self.owner, "other", "local"))
        authority = self.sessions.session_operation_authority
        context = authority.acquire(
            session_id=other.id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=self.sessions.session_operation_owner_instance_id,
            lease_seconds=600,
        )
        record = asyncio.run(self.blobs.create_blob(other.id, filename, b"foreign prompt", "text/plain", session_operation_context=context))
        return record.id, context


@pytest.fixture
def make_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "evidence-batch-test-key")
    built: list[_Rig] = []

    def _make(n_files: int) -> _Rig:
        data_dir = tmp_path / f"data-{n_files}-{len(built)}"
        data_dir.mkdir()
        engine = create_session_engine(f"sqlite:///{data_dir / 'sessions.db'}")
        initialize_session_schema(engine)
        counter = _Counter(engine)
        sessions = SessionServiceImpl(
            engine,
            data_dir,
            telemetry=build_sessions_telemetry(),
            log=structlog.get_logger("evidence-batch"),
            session_operation_lease_seconds=600,
        )
        owner = f"evidence-batch-{uuid.uuid4().hex[:12]}"
        with engine.begin() as conn:
            ensure_test_identity(conn, identity_id=owner)
        session = asyncio.run(sessions.create_session(owner, "evidence batch", "local"))
        context = sessions.session_operation_authority.acquire(
            session_id=session.id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=sessions.session_operation_owner_instance_id,
            lease_seconds=600,
        )
        blobs = BlobServiceImpl(engine, data_dir, session_operation_authority=sessions.session_operation_authority)
        blob_ids = [
            asyncio.run(
                blobs.create_blob(
                    session.id,
                    f"q{index:02d}.txt",
                    f"Judge the record against criterion {index:02d}.".encode(),
                    "text/plain",
                    session_operation_context=context,
                )
            ).id
            for index in range(n_files)
        ]
        source_path = data_dir / "blobs" / str(session.id) / "input.csv"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text("text\nfirst record\n", encoding="utf-8")
        secrets = WebSecretService(
            user_store=UserSecretStore(engine=engine, master_key="evidence-batch-master-key-32-chars"),
            server_store=ServerSecretStore(allowlist=()),
        )
        service = ComposerServiceImpl.for_trained_operator(
            catalog=CatalogServiceImpl(get_shared_plugin_manager()),
            settings=_make_settings(data_dir=data_dir),
            blob_service=blobs,
            secret_service=ScopedSecretResolver(secrets, auth_provider_type="local"),
        )
        rig = _Rig(engine, counter, sessions, blobs, service, context, owner, session.id, data_dir, blob_ids)
        built.append(rig)
        return rig

    yield _make
    for rig in built:
        rig.sessions.session_operation_authority.release(rig.context)
        rig.engine.dispose()


def test_the_counter_counts_what_runs(make_rig: Any) -> None:
    """Control: one 7-statement transaction, three 1-statement transactions, and nothing."""
    rig = make_rig(0)
    with rig.counter.phase() as one, rig.engine.begin() as conn:
        for _ in range(7):
            conn.execute(text("SELECT 1"))
    with rig.counter.phase() as three:
        for _ in range(3):
            with rig.engine.begin() as conn:
                conn.execute(text("SELECT 1"))
    with rig.counter.phase() as none:
        pass
    assert (one.transactions, one.statements) == (1, 7)
    assert (three.transactions, three.statements) == (3, 3)
    assert none == _Phase()


@pytest.mark.parametrize("n_files", [1, 32, 70])
def test_one_evidence_read_is_one_fenced_transaction_at_any_n(make_rig: Any, n_files: int) -> None:
    rig = make_rig(n_files)
    state = rig.state()

    with rig.counter.phase() as read:
        evidence = rig.service._preflight._read_preflight_evidence(state, rig.context, rig.owner, str(rig.session_id))

    assert not isinstance(evidence, str), evidence
    assert [blob.record.id for blob in evidence.blobs] == sorted(rig.blob_ids)
    # The fenced batch and the secret inventory read; one fence check, which is the only write.
    assert (read.transactions, read.fence_checks, read.writes) == (2, 1, 1)
    assert len(read.blob_row_ids) == n_files


def _cached_preflight(rig: _Rig, state: CompositionState) -> Callable[[], ValidationResult]:
    """One compose round's cached runtime preflight of ``state`` (reuse on)."""
    snapshot, _view = rig.service._policy_context.build(rig.owner)
    cache = rig.service._preflight.new_cache(mutation_epoch=MutationEpoch())

    def cached() -> ValidationResult:
        return asyncio.run(
            rig.service._preflight.cached_runtime_preflight(
                state,
                user_id=rig.owner,
                session_id=str(rig.session_id),
                cache=cache,
                initial_version=state.version,
                session_scope="session:evidence-batch",
                plugin_snapshot=snapshot,
                session_operation_context=rig.context,
            )
        )

    return cached


@pytest.mark.parametrize("n_files", [1, 32, 70])
def test_a_reuse_hit_is_two_transactions_and_one_fence_write(make_rig: Any, n_files: int) -> None:
    rig = make_rig(n_files)
    cached = _cached_preflight(rig, rig.state())

    with rig.counter.phase() as first_call:
        first = cached()
    with rig.counter.phase() as hit:
        second = cached()

    assert second == first
    # The first call computed: its own reads go row by row, as on base, plus two bracket batches.
    assert first_call.fence_checks > 2
    assert (hit.transactions, hit.fence_checks, hit.writes) == (2, 1, 1)
    assert len(hit.blob_row_ids) == n_files


@pytest.mark.parametrize(("change", "computes_expected"), [(True, 2), (False, 1)])
def test_a_record_changed_between_the_before_and_after_batches_is_not_stored(
    make_rig: Any, monkeypatch: pytest.MonkeyPatch, change: bool, computes_expected: int
) -> None:
    """The last record in batch order changes after the compute's own reads, before the after-batch.

    The after-batch must see it: the verdict is returned but not stored, so
    the next preflight computes again rather than reusing it. Control: with
    no change the second preflight is a reuse.
    """
    rig = make_rig(3)
    last = max(rig.blob_ids)
    computes: list[int] = []
    real_validate = preflight_module.validate_pipeline

    def validate_then_change_last_row(*args: Any, **kwargs: Any) -> ValidationResult:
        result = real_validate(*args, **kwargs)
        computes.append(1)
        if change and len(computes) == 1:
            with rig.engine.begin() as conn:
                conn.execute(text("UPDATE blobs SET filename = 'renamed.txt' WHERE id = :id"), {"id": str(last)})
        return result

    monkeypatch.setattr(preflight_module, "validate_pipeline", validate_then_change_last_row)
    cached = _cached_preflight(rig, rig.state())

    cached()
    cached()

    assert len(computes) == computes_expected


@pytest.mark.parametrize("stray", ["outside_the_session", "missing"])
def test_a_custody_miss_declines_the_whole_evidence_read(make_rig: Any, stray: str) -> None:
    """One record the fence cannot vouch for declines the read, as one failing single read did."""
    rig = make_rig(3)
    if stray == "outside_the_session":
        stray_id, other_context = rig.new_session_blob("foreign.txt")
        rig.sessions.session_operation_authority.release(other_context)
        stray_marker = {"blob_ref": str(stray_id), "mode": "inline_content", "sha256": "0" * 64}
    else:
        stray_id = uuid.uuid4()
        stray_marker = {"blob_ref": str(stray_id), "mode": "inline_content", "sha256": "0" * 64}
    state = rig.state(extra_markers=(stray_marker,))

    evidence = rig.service._preflight._read_preflight_evidence(state, rig.context, rig.owner, str(rig.session_id))

    assert evidence == "evidence_read_failed"


@pytest.mark.parametrize("stray", ["outside_the_session", "missing"])
def test_the_batch_fails_whole_on_a_custody_miss_naming_the_id(make_rig: Any, stray: str) -> None:
    rig = make_rig(3)
    if stray == "outside_the_session":
        stray_id, other_context = rig.new_session_blob("foreign.txt")
        rig.sessions.session_operation_authority.release(other_context)
    else:
        stray_id = uuid.uuid4()

    with rig.counter.phase() as read, pytest.raises(BlobNotFoundError) as missing:
        rig.blobs.get_blobs_sync([*rig.blob_ids, stray_id], rig.context)

    assert str(stray_id) in str(missing.value)
    assert (read.transactions, read.fence_checks) == (1, 1)


def test_the_batch_is_read_under_the_exact_fence(make_rig: Any) -> None:
    """A context whose epoch is no longer the fence's is refused before any record is returned."""
    rig = make_rig(2)
    stale = replace(rig.context, fence=replace(rig.context.fence, operation_epoch=rig.context.fence.operation_epoch + 1))

    with rig.counter.phase() as read, pytest.raises(SessionOperationFenceLost):
        rig.blobs.get_blobs_sync(rig.blob_ids, stale)

    assert read.fence_checks == 1
    assert read.blob_row_ids == []


def test_rows_are_read_and_returned_in_ascending_id_order_once_each(make_rig: Any) -> None:
    rig = make_rig(5)
    ascending = sorted(rig.blob_ids)
    requested = [*reversed(ascending), ascending[0]]

    with rig.counter.phase() as read:
        records = rig.blobs.get_blobs_sync(requested, rig.context)

    assert [record.id for record in records] == ascending
    assert read.blob_row_ids == [str(blob_id) for blob_id in ascending]
    assert (read.transactions, read.fence_checks) == (1, 1)


def test_no_ids_open_no_transaction(make_rig: Any) -> None:
    rig = make_rig(1)

    with rig.counter.phase() as read:
        records = rig.blobs.get_blobs_sync([], rig.context)

    assert records == ()
    assert read == _Phase()
