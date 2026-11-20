"""Round-scoped reuse of runtime-preflight verdicts for blob-bearing states.

Before reuse existed, every runtime preflight of a state holding a blob
binding recomputed, because blob status can change without a state version
change. A compose round on a graph whose prompts are held in files ran three
to five identical preflights on one state object. The round cache now hands
back a returned verdict when, and only when:

* the state is the identical object (``is``), not an equal one;
* no mutation-kind tool call has ended since the verdict was computed;
* every referenced blob record, the stat of its stored file (size, mtime,
  ctime, inode) and the user's secret inventory read the same as they did
  immediately before and after the compute, and every blob row the compute's
  own reads observed equals that evidence;
* the state wires no secret (a wired secret's value is not evidence);
* the caller's deadline has not passed.

The rule every test here pins: a reused verdict equals what a fresh compute
would return or raise. Failures are never stored; a reuse hands out a copy.
A plain ``dict`` cache, a missing blob service or operation context, and the
switch turned off all take the base path.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.blobs import BlobIntegrityError, BlobNotFoundError, BlobRecord, BlobStateError
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.secrets import SecretInventoryItem
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web import config as web_config
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer import composer_preflight as preflight_module
from elspeth.web.composer import discovery_cache
from elspeth.web.composer.discovery_cache import RuntimePreflightCache
from elspeth.web.composer.protocol import ComposerRuntimePreflightError
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, SourceSpec
from elspeth.web.composer.tools import _dispatch
from elspeth.web.composer.tools._dispatch import _ALL_MUTATION_TOOL_NAMES, execute_tool
from elspeth.web.execution.schemas import (
    CHECK_PATH_ALLOWLIST,
    ValidationCheck,
    ValidationReadiness,
    ValidationResult,
    ValidationWarning,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

from ._helpers import _empty_state, _make_settings, _mock_catalog

pytestmark = pytest.mark.anyio


def _verdict(compute_number: int) -> ValidationResult:
    """A result labelled with the compute that produced it, so equality tells computes apart."""
    return ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        warnings=[
            ValidationWarning(
                component_id=None,
                component_type=None,
                message=f"compute {compute_number}",
                suggestion=None,
                warning_code="test_compute_label",
            )
        ],
        readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
    )


class _RecordingValidatePipeline:
    """Stands in for ``validate_pipeline``: counts computes, can raise.

    ``read_ids`` are blob ids read through the preflight's own metadata reader
    on every compute, as the real materialiser does for inline-content refs.
    ``during`` runs inside the compute, after those reads.
    """

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls = 0
        self.returned: list[ValidationResult] = []
        self.read_ids: tuple[UUID, ...] = ()
        self.content_ids: tuple[UUID, ...] = ()
        self.content_failures: list[str] = []
        self.during: Callable[[], None] | None = None
        self._raises = raises

    def __call__(self, *_args: Any, **kwargs: Any) -> ValidationResult:
        self.calls += 1
        for blob_id in self.read_ids:
            kwargs["blob_get_metadata"](blob_id)
        for blob_id in self.content_ids:
            # The real materialiser turns these two content-read failures
            # into violations and still returns a verdict.
            try:
                kwargs["blob_get_content"](blob_id)
            except (BlobNotFoundError, BlobStateError) as exc:
                self.content_failures.append(type(exc).__name__)
        if self.during is not None:
            self.during()
        if self._raises is not None:
            raise self._raises
        result = _verdict(self.calls)
        self.returned.append(result)
        return result


@dataclass
class _BlobRows:
    """A blob service that serves metadata rows from a dict, as ``get_blob_sync`` does.

    ``transient`` replaces the row returned by exactly one read (by 1-based
    read number), which models a change that happens and reverts while a
    compute is running. Reads are numbered per RECORD across both paths: a
    ``get_blobs_sync`` batch (the evidence read, one fenced transaction in
    the real service) gives each of its records the next number, in
    ascending id order, as the compute's single reads do. ``batches`` lists
    each batch's ids, so a test can tell the evidence reads from the
    compute's own.
    """

    records: dict[UUID, BlobRecord]
    reads: int = 0
    transient: dict[int, BlobRecord] = field(default_factory=dict)
    contexts: list[SessionOperationContext] = field(default_factory=list)
    batches: list[tuple[UUID, ...]] = field(default_factory=list)
    # When set, every read first waits (bounded) for the event: a slow store.
    gate: threading.Event | None = None
    # When True, the content read finds no stored content.
    content_missing: bool = False

    def get_blob_sync(self, blob_id: UUID, context: SessionOperationContext) -> BlobRecord:
        if self.gate is not None:
            self.gate.wait(5)
        self.reads += 1
        self.contexts.append(context)
        if self.reads in self.transient:
            return self.transient[self.reads]
        if blob_id not in self.records:
            raise BlobNotFoundError(str(blob_id))
        return self.records[blob_id]

    def get_blobs_sync(self, blob_ids: Iterable[UUID], context: SessionOperationContext) -> tuple[BlobRecord, ...]:
        """The real service's batch: ascending ids, the first miss fails the whole read, no ids no read."""
        ordered = tuple(sorted(set(blob_ids)))
        if not ordered:
            return ()
        self.batches.append(ordered)
        return tuple(self.get_blob_sync(blob_id, context) for blob_id in ordered)

    def read_blob_content_sync(self, blob_id: UUID, context: SessionOperationContext) -> tuple[BlobRecord, bytes]:
        """The real service's content read: the row plus the stored bytes, hash-checked."""
        record = self.get_blob_sync(blob_id, context)
        if self.content_missing:
            raise BlobNotFoundError(str(blob_id))
        content = Path(record.storage_path).read_bytes()
        if hashlib.sha256(content).hexdigest() != record.content_hash:
            raise BlobIntegrityError(str(blob_id), expected=record.content_hash, actual=hashlib.sha256(content).hexdigest())
        return record, content


class _RecordingCounter:
    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, object]]] = []

    def add(self, value: int, attributes: dict[str, object]) -> None:
        self.calls.append((value, dict(attributes)))


@dataclass
class _Rig:
    service: ComposerServiceImpl
    blobs: _BlobRows
    view: PolicyCatalogView
    snapshot: PluginAvailabilitySnapshot
    cache: RuntimePreflightCache
    context: SessionOperationContext
    session_id: UUID
    blob_id: UUID
    storage: Path
    state: CompositionState

    async def preflight(
        self,
        state: CompositionState | None = None,
        *,
        tolerant: bool = False,
        without_context: bool = False,
        context: SessionOperationContext | None = None,
        deadline: float | None = None,
    ) -> ValidationResult:
        target = self.state if state is None else state
        return await self.service._preflight.cached_runtime_preflight(
            target,
            user_id="user-1",
            session_id=str(self.session_id),
            cache=self.cache,
            initial_version=target.version,
            session_scope="session:test",
            plugin_snapshot=self.snapshot,
            session_operation_context=None if without_context else (context or self.context),
            interpretation_tolerant=tolerant,
            deadline=deadline,
        )


def _record(blob_id: UUID, session_id: UUID, storage: Path, body: bytes) -> BlobRecord:
    return BlobRecord(
        id=blob_id,
        session_id=session_id,
        filename="prompt.txt",
        mime_type="text/plain",
        size_bytes=len(body),
        content_hash="a" * 64,
        storage_path=str(storage),
        created_at=datetime(2026, 11, 20, tzinfo=UTC),
        created_by="user",
        source_description=None,
        status="ready",
        creation_modality=CreationModality.VERBATIM,
        created_from_message_id=None,
        creating_model_identifier=None,
        creating_model_version=None,
        creating_provider=None,
        creating_composer_skill_hash=None,
        creating_arguments_hash=None,
    )


def _blob_state(blob_id: UUID) -> CompositionState:
    return _empty_state().with_source(
        SourceSpec(
            plugin="csv",
            on_success="out",
            options={"path": {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": "a" * 64}},
            on_validation_failure="discard",
        )
    )


def _build_rig(tmp_path: Path, *, reuse_enabled: bool = True, with_blob_service: bool = True) -> _Rig:
    session_id = uuid4()
    blob_id = uuid4()
    storage = tmp_path / "blobs" / "prompt.txt"
    storage.parent.mkdir(parents=True, exist_ok=True)
    body = b"Assess {{ row.text }} carefully."
    storage.write_bytes(body)
    blobs = _BlobRows(records={blob_id: _record(blob_id, session_id, storage, body)})
    service = ComposerServiceImpl.for_trained_operator(
        catalog=_mock_catalog(),
        settings=_make_settings(data_dir=tmp_path, composer_validation_reuse_enabled=reuse_enabled),
        blob_service=blobs if with_blob_service else None,
    )
    snapshot, view = service._policy_context.build("user-1")
    return _Rig(
        service=service,
        blobs=blobs,
        view=view,
        snapshot=snapshot,
        cache=service._preflight.new_cache(mutation_epoch=view.mutation_epoch),
        context=SessionOperationContext(
            fence=SessionOperationFence(str(session_id), "operation-1", "lease-1", 1),
            operation_kind=SessionOperationKind.COMPOSE,
        ),
        session_id=session_id,
        blob_id=blob_id,
        storage=storage,
        state=_blob_state(blob_id),
    )


@pytest.fixture
def rig(tmp_path: Path) -> _Rig:
    return _build_rig(tmp_path)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _RecordingValidatePipeline:
    recording = _RecordingValidatePipeline()
    monkeypatch.setattr(preflight_module, "validate_pipeline", recording)
    return recording


# T1 ------------------------------------------------------------------------


async def test_same_state_same_evidence_same_epoch_reuses_the_returned_verdict(
    rig: _Rig, fake: _RecordingValidatePipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    reused = _RecordingCounter()
    monkeypatch.setattr(preflight_module, "VALIDATION_REUSE_COUNTER", reused)
    assert isinstance(rig.cache, discovery_cache.ComposeRoundPreflightCache)

    first = await rig.preflight()
    second = await rig.preflight()
    third = await rig.preflight()

    assert fake.calls == 1
    assert first is fake.returned[0]
    assert second == first and third == first
    assert reused.calls == [
        (1, {"layer": "runtime_preflight", "interpretation_tolerant": False}),
        (1, {"layer": "runtime_preflight", "interpretation_tolerant": False}),
    ]
    # Every read went through the preflight's own operation context.
    assert rig.blobs.contexts and all(context is rig.context for context in rig.blobs.contexts)


async def test_strict_and_tolerant_verdicts_are_stored_apart(rig: _Rig, fake: _RecordingValidatePipeline) -> None:
    strict = await rig.preflight()
    tolerant = await rig.preflight(tolerant=True)

    assert fake.calls == 2
    assert strict != tolerant  # labelled by compute
    assert await rig.preflight() == strict
    assert await rig.preflight(tolerant=True) == tolerant
    assert fake.calls == 2


async def test_an_equal_but_distinct_state_object_recomputes(rig: _Rig, fake: _RecordingValidatePipeline) -> None:
    twin = replace(rig.state)
    assert twin == rig.state and twin is not rig.state

    first = await rig.preflight()
    second = await rig.preflight(twin)

    assert fake.calls == 2
    assert second is not first


# T2 ------------------------------------------------------------------------


async def test_a_status_flip_between_preflights_recomputes_and_returns_the_new_verdict(rig: _Rig, fake: _RecordingValidatePipeline) -> None:
    first = await rig.preflight()
    rig.blobs.records[rig.blob_id] = replace(rig.blobs.records[rig.blob_id], status="pending")

    second = await rig.preflight()

    assert fake.calls == 2
    assert second is fake.returned[1]
    assert second is not first


# T3 ------------------------------------------------------------------------


def _rewrite_same_length(storage: Path) -> None:
    body = storage.read_bytes()
    stat = storage.stat()
    storage.write_bytes(body.upper())
    # A rewrite of the bound prompt file that keeps its size: only the stat's
    # mtime tells, so pin it to move even on a coarse-clock filesystem.
    os.utime(storage, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


def _rewrite_same_length_mtime_restored(storage: Path) -> None:
    """F2: rewrite in place, same size, same inode, with the old mtime put back.

    Size, mtime and inode all read as before; only the change time, which no
    writer can set back, shows the rewrite.
    """
    body = storage.read_bytes()
    stat = storage.stat()
    with storage.open("r+b") as handle:
        handle.write(body.upper())
    os.utime(storage, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = storage.stat()
    assert (after.st_size, after.st_mtime_ns, after.st_ino) == (stat.st_size, stat.st_mtime_ns, stat.st_ino)
    assert after.st_ctime_ns != stat.st_ctime_ns


def _replace_file(storage: Path) -> None:
    replacement = storage.with_name("replacement.txt")
    replacement.write_bytes(storage.read_bytes())
    stat = storage.stat()
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    os.replace(replacement, storage)


_RECORD_CHANGES = [
    pytest.param({"content_hash": "b" * 64}, id="content_hash"),
    pytest.param({"size_bytes": 1}, id="size_bytes"),
    pytest.param({"mime_type": "text/csv"}, id="mime_type"),
    pytest.param({"creation_modality": CreationModality.LLM_GENERATED}, id="creation_modality"),
    pytest.param({"created_by": "assistant"}, id="created_by"),
    pytest.param({"session_id": UUID(int=7)}, id="session_id"),
    pytest.param({"filename": "other.txt"}, id="filename"),
]


@pytest.mark.parametrize("change", _RECORD_CHANGES)
async def test_a_changed_blob_record_field_recomputes(rig: _Rig, fake: _RecordingValidatePipeline, change: dict[str, object]) -> None:
    first = await rig.preflight()
    rig.blobs.records[rig.blob_id] = replace(rig.blobs.records[rig.blob_id], **change)

    second = await rig.preflight()

    assert fake.calls == 2
    assert second is not first


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda storage: storage.write_bytes(storage.read_bytes() + b" More."), id="bytes_appended"),
        pytest.param(_rewrite_same_length, id="bytes_rewritten_same_length"),
        pytest.param(_rewrite_same_length_mtime_restored, id="bytes_rewritten_same_length_mtime_restored"),
        pytest.param(_replace_file, id="file_replaced_same_mtime"),
    ],
)
async def test_a_changed_bound_prompt_file_recomputes_although_its_record_is_unchanged(
    rig: _Rig, fake: _RecordingValidatePipeline, change: Any
) -> None:
    record_before = rig.blobs.records[rig.blob_id]
    first = await rig.preflight()
    change(rig.storage)

    second = await rig.preflight()

    assert rig.blobs.records[rig.blob_id] == record_before
    assert fake.calls == 2
    assert second is not first


async def test_a_record_that_disappears_recomputes(rig: _Rig, fake: _RecordingValidatePipeline) -> None:
    await rig.preflight()
    del rig.blobs.records[rig.blob_id]

    await rig.preflight()

    assert fake.calls == 2


# T4 ------------------------------------------------------------------------


async def test_without_a_blob_service_every_preflight_recomputes(tmp_path: Path, fake: _RecordingValidatePipeline) -> None:
    rig = _build_rig(tmp_path, with_blob_service=False)

    await rig.preflight()
    await rig.preflight()

    assert fake.calls == 2


async def test_without_an_operation_context_every_preflight_recomputes(rig: _Rig, fake: _RecordingValidatePipeline) -> None:
    await rig.preflight(without_context=True)
    await rig.preflight(without_context=True)

    assert fake.calls == 2
    assert rig.blobs.reads == 0


async def test_a_plain_dict_cache_never_reuses_a_blob_verdict(rig: _Rig, fake: _RecordingValidatePipeline) -> None:
    rig.cache = rig.service._preflight.new_cache()
    assert type(rig.cache) is dict

    await rig.preflight()
    await rig.preflight()

    assert fake.calls == 2
    assert rig.cache == {}


async def test_a_blob_ref_that_is_not_a_uuid_ends_reuse(rig: _Rig, fake: _RecordingValidatePipeline) -> None:
    source = rig.state.sources["source"]
    state = replace(
        rig.state,
        sources={"source": replace(source, options={**dict(source.options), "extra": {"blob_ref": "not-a-uuid"}})},
    )

    await rig.preflight(state)
    await rig.preflight(state)

    assert fake.calls == 2


# T5 ------------------------------------------------------------------------


async def test_evidence_that_changes_during_the_compute_is_not_stored(rig: _Rig, fake: _RecordingValidatePipeline) -> None:
    """Read 1 brackets the compute from before, read 2 from after.

    The row read after the compute differs, then reverts: the evidence the
    verdict was judged on cannot be vouched for, so nothing is stored, and the
    next preflight recomputes although its evidence equals the first read.
    """
    original = rig.blobs.records[rig.blob_id]
    rig.blobs.transient[2] = replace(original, status="pending")

    await rig.preflight()
    # Sequence: read 1 is the before-batch, read 2 the after-batch; each
    # bracket is one ``get_blobs_sync`` (one fenced transaction for all ids).
    assert rig.blobs.reads == 2
    assert rig.blobs.batches == [(rig.blob_id,), (rig.blob_id,)]
    second = await rig.preflight()

    assert fake.calls == 2
    assert second is fake.returned[1]


# T6 ------------------------------------------------------------------------


async def test_a_failed_compute_is_not_reused(rig: _Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    failing = _RecordingValidatePipeline(raises=ValueError("synthetic preflight failure"))
    monkeypatch.setattr(preflight_module, "validate_pipeline", failing)

    with pytest.raises(ComposerRuntimePreflightError) as first:
        await rig.preflight()
    with pytest.raises(ComposerRuntimePreflightError) as second:
        await rig.preflight()

    assert failing.calls == 2
    assert type(first.value.original_exc) is type(second.value.original_exc) is ValueError
    assert str(first.value.original_exc) == str(second.value.original_exc) == "synthetic preflight failure"
    assert isinstance(rig.cache, discovery_cache.ComposeRoundPreflightCache)
    assert rig.cache.blob_verdicts == {}


# T7 and the mutation counter -------------------------------------------------


async def test_a_secret_mutation_tool_between_two_preflights_recomputes(rig: _Rig, fake: _RecordingValidatePipeline) -> None:
    """``wire_secret_ref`` can change a secret that no state key sees."""
    assert "wire_secret_ref" in _ALL_MUTATION_TOOL_NAMES
    first = await rig.preflight()
    epoch = rig.view.mutation_epoch.value

    result = execute_tool("wire_secret_ref", {}, rig.state, rig.view, plugin_snapshot=rig.snapshot)

    assert result.success is False  # refused: there is no secret service here
    assert rig.view.mutation_epoch.value == epoch + 1
    second = await rig.preflight()
    assert fake.calls == 2
    assert second is not first


async def test_a_state_mutation_tool_between_two_preflights_on_one_state_recomputes(rig: _Rig, fake: _RecordingValidatePipeline) -> None:
    first = await rig.preflight()

    result = execute_tool("patch_node_options", {"node_id": "absent", "patch": {"x": 1}}, rig.state, rig.view, plugin_snapshot=rig.snapshot)

    assert result.success is False
    second = await rig.preflight()
    assert fake.calls == 2
    assert second is not first


async def test_a_mutation_tool_that_raises_still_ends_reuse(
    rig: _Rig, fake: _RecordingValidatePipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raising_gate(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("synthetic failure after the handler ran")

    # The registries are read-only; the interpretation gate runs right after
    # the handler inside the same call, so a raise there is a raise of the call.
    monkeypatch.setattr(_dispatch, "_enforce_composition_interpretation_gate", raising_gate)
    await rig.preflight()

    with pytest.raises(RuntimeError, match="synthetic failure after the handler ran"):
        execute_tool("patch_node_options", {"node_id": "absent", "patch": {"x": 1}}, rig.state, rig.view, plugin_snapshot=rig.snapshot)

    await rig.preflight()
    assert fake.calls == 2


async def test_a_discovery_tool_between_two_preflights_keeps_the_reuse(rig: _Rig, fake: _RecordingValidatePipeline) -> None:
    """Negative control for the three tests above: the epoch is not bumped by every call."""
    first = await rig.preflight()
    epoch = rig.view.mutation_epoch.value

    execute_tool("get_pipeline_state", {}, rig.state, rig.view, plugin_snapshot=rig.snapshot)

    assert rig.view.mutation_epoch.value == epoch
    assert await rig.preflight() == first
    assert fake.calls == 1


# T10 -------------------------------------------------------------------------


async def test_reuse_switched_off_recomputes_every_blob_preflight(tmp_path: Path, fake: _RecordingValidatePipeline) -> None:
    rig = _build_rig(tmp_path, reuse_enabled=False)

    first = await rig.preflight()
    second = await rig.preflight()

    assert fake.calls == 2
    assert second != first
    # Switched off, a round gets the base plain mapping, not the reuse store.
    assert type(rig.cache) is dict
    assert rig.cache == {}
    assert rig.blobs.reads == 0


@pytest.mark.parametrize(("env_value", "expected"), [("false", False), ("true", True), (None, True)])
def test_the_switch_is_read_from_the_environment_and_defaults_on(
    monkeypatch: pytest.MonkeyPatch, env_value: str | None, expected: bool
) -> None:
    for name, value in {
        "ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS": "15",
        "ELSPETH_WEB__COMPOSER_CONTEXT_MAX_PROMPT_TOKENS": "200000",
        "ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS": "10",
        "ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS": "85.0",
        "ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE": "10",
        "ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY": "0" * 64,
    }.items():
        monkeypatch.setenv(name, value)
    if env_value is None:
        monkeypatch.delenv("ELSPETH_WEB__COMPOSER_VALIDATION_REUSE_ENABLED", raising=False)
    else:
        monkeypatch.setenv("ELSPETH_WEB__COMPOSER_VALIDATION_REUSE_ENABLED", env_value)

    assert web_config.settings_from_env().composer_validation_reuse_enabled is expected


async def test_a_round_store_handed_in_with_reuse_switched_off_is_never_used(tmp_path: Path, fake: _RecordingValidatePipeline) -> None:
    """The switch is honoured by the preflight itself, not only by ``new_cache``."""
    rig = _build_rig(tmp_path, reuse_enabled=False)
    rig.cache = discovery_cache.ComposeRoundPreflightCache(mutation_epoch=rig.view.mutation_epoch)

    await rig.preflight()
    await rig.preflight()

    assert fake.calls == 2
    assert rig.cache.blob_verdicts == {}
    assert rig.blobs.reads == 0


# Round 2 -------------------------------------------------------------------
# Each test below pins one way a reuse could differ from a fresh compute.


def _declined(monkeypatch: pytest.MonkeyPatch) -> _RecordingCounter:
    counter = _RecordingCounter()
    monkeypatch.setattr(preflight_module, "PREFLIGHT_REUSE_DECLINED_COUNTER", counter)
    return counter


def _reused(monkeypatch: pytest.MonkeyPatch) -> _RecordingCounter:
    counter = _RecordingCounter()
    monkeypatch.setattr(preflight_module, "VALIDATION_REUSE_COUNTER", counter)
    return counter


class _Inventory:
    """``list_refs`` as the scoped resolver serves it, from a list the test edits."""

    def __init__(self) -> None:
        self.items: list[SecretInventoryItem] = []
        self.reads = 0

    def list_refs(self, user_id: str) -> list[SecretInventoryItem]:
        assert user_id == "user-1"
        self.reads += 1
        return list(self.items)


# F1: the secret inventory is evidence; a wired secret is never reused.


async def test_a_secret_created_between_two_preflights_recomputes(
    rig: _Rig, fake: _RecordingValidatePipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = _Inventory()
    monkeypatch.setattr(rig.service._preflight, "_secret_service", inventory)
    await rig.preflight()
    await rig.preflight()
    assert fake.calls == 1  # control: an unchanged inventory reuses

    inventory.items.append(SecretInventoryItem(name="NEW_KEY", scope="user", available=True, source_kind="user_store", reason=None))
    await rig.preflight()

    assert fake.calls == 2
    assert inventory.reads >= 4  # bracket before/after, the hit, the miss


async def test_a_secret_deleted_between_two_preflights_recomputes(
    rig: _Rig, fake: _RecordingValidatePipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = _Inventory()
    inventory.items.append(SecretInventoryItem(name="OLD_KEY", scope="user", available=True, source_kind="user_store", reason=None))
    monkeypatch.setattr(rig.service._preflight, "_secret_service", inventory)
    await rig.preflight()

    inventory.items.clear()
    await rig.preflight()

    assert fake.calls == 2


@pytest.mark.parametrize(
    "wired",
    [
        pytest.param({"secret_ref": "SINK_KEY"}, id="marker"),
        pytest.param("${SINK_KEY}", id="env_ref_for_a_listed_name"),
    ],
)
async def test_a_state_that_wires_a_secret_is_never_reused(
    rig: _Rig, fake: _RecordingValidatePipeline, monkeypatch: pytest.MonkeyPatch, wired: object
) -> None:
    """The preflight resolves a wired secret's value, which no evidence names."""
    inventory = _Inventory()
    inventory.items.append(SecretInventoryItem(name="SINK_KEY", scope="user", available=True, source_kind="user_store", reason=None))
    monkeypatch.setattr(rig.service._preflight, "_secret_service", inventory)
    declined = _declined(monkeypatch)
    source = rig.state.sources["source"]
    state = replace(rig.state, sources={"source": replace(source, options={**dict(source.options), "api_key": wired})})

    await rig.preflight(state)
    await rig.preflight(state)

    assert fake.calls == 2
    assert declined.calls == [(1, {"reason": "secret_ref_sites"})] * 2


# F2 is the ``bytes_rewritten_same_length_mtime_restored`` case above.

# F3: the compute's own reads must agree with the evidence.


async def test_a_row_that_changes_and_reverts_inside_the_compute_is_not_stored(
    rig: _Rig, fake: _RecordingValidatePipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reads 1 and 3 bracket the compute and agree; read 2 is the compute's own and saw another row."""
    declined = _declined(monkeypatch)
    fake.read_ids = (rig.blob_id,)
    rig.blobs.transient[2] = replace(rig.blobs.records[rig.blob_id], status="pending")

    await rig.preflight()
    # Sequence: read 1 the before-batch, read 2 the compute's own single
    # read, read 3 the after-batch; only reads 1 and 3 are batches.
    assert rig.blobs.reads == 3
    assert rig.blobs.batches == [(rig.blob_id,), (rig.blob_id,)]
    await rig.preflight()

    assert fake.calls == 2
    assert declined.calls[0] == (1, {"reason": "evidence_changed"})


async def test_a_compute_whose_own_reads_match_the_evidence_is_stored(rig: _Rig, fake: _RecordingValidatePipeline) -> None:
    """Control for the test above: the same reads with no change are reused."""
    fake.read_ids = (rig.blob_id,)

    first = await rig.preflight()
    second = await rig.preflight()

    assert fake.calls == 1
    assert second == first


async def test_a_compute_whose_content_read_failed_is_not_stored(
    rig: _Rig, fake: _RecordingValidatePipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The content read fails inside the compute; the compute still returns a verdict."""
    declined = _declined(monkeypatch)
    rig.blobs.content_missing = True
    fake.content_ids = (rig.blob_id,)

    await rig.preflight()
    await rig.preflight()

    assert fake.content_failures == ["BlobNotFoundError"] * 2
    assert fake.calls == 2
    assert declined.calls[0] == (1, {"reason": "evidence_changed"})


async def test_a_compute_whose_content_read_succeeded_is_stored(rig: _Rig, fake: _RecordingValidatePipeline) -> None:
    """Control for the test above: the content read succeeds and the verdict is reused."""
    rig.blobs.records[rig.blob_id] = replace(
        rig.blobs.records[rig.blob_id], content_hash=hashlib.sha256(rig.storage.read_bytes()).hexdigest()
    )
    fake.content_ids = (rig.blob_id,)

    await rig.preflight()
    await rig.preflight()

    assert fake.content_failures == []
    assert fake.calls == 1


# F4: a reuse honours the caller's deadline exactly as a fresh compute does.


async def test_a_reuse_after_the_deadline_raises_the_timeout_a_fresh_preflight_raises(
    tmp_path: Path, rig: _Rig, fake: _RecordingValidatePipeline
) -> None:
    await rig.preflight()
    fresh = _build_rig(tmp_path / "fresh", reuse_enabled=False)
    expired = asyncio.get_running_loop().time() - 1.0

    with pytest.raises(ComposerRuntimePreflightError) as reused:
        await rig.preflight(deadline=expired)
    with pytest.raises(ComposerRuntimePreflightError) as computed:
        await fresh.preflight(deadline=expired)

    assert type(reused.value.original_exc) is type(computed.value.original_exc) is TimeoutError
    assert str(reused.value.original_exc) == str(computed.value.original_exc)
    assert fake.calls == 1  # the stored verdict was not handed out and nothing new ran
    await rig.preflight()
    assert fake.calls == 1  # the stored verdict is still good for a caller with budget


async def test_an_evidence_read_that_outlasts_the_budget_raises_the_timeout(rig: _Rig, fake: _RecordingValidatePipeline) -> None:
    """A hit whose evidence read runs past the deadline is not handed out; the base path times out."""
    await rig.preflight()
    rig.blobs.gate = threading.Event()
    try:
        with pytest.raises(ComposerRuntimePreflightError) as raised:
            await rig.preflight(deadline=asyncio.get_running_loop().time() + 0.05)
    finally:
        rig.blobs.gate.set()
    assert type(raised.value.original_exc) is TimeoutError


# F6: no caller can change the verdict another caller is handed.


async def test_a_caller_cannot_change_the_verdict_a_later_reuse_hands_out(rig: _Rig, fake: _RecordingValidatePipeline) -> None:
    first = await rig.preflight()
    pristine = first.model_copy(deep=True)

    first.warnings.clear()  # the computing caller edits its own result
    second = await rig.preflight()
    assert second == pristine
    second.warnings.clear()  # a reusing caller edits its own copy
    second.checks.append(
        ValidationCheck(name=CHECK_PATH_ALLOWLIST, passed=True, detail="edited by a caller", affected_nodes=(), outcome_code=None)
    )
    third = await rig.preflight()

    assert third == pristine
    assert fake.calls == 1


# F7: mutants the first review found surviving.


async def test_a_verdict_whose_compute_saw_a_mutation_end_is_not_stored(
    rig: _Rig, fake: _RecordingValidatePipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mutation-kind tool call that ends while the compute runs leaves nothing in the store."""
    declined = _declined(monkeypatch)
    fake.during = rig.view.record_mutation_tool_end

    await rig.preflight()

    assert isinstance(rig.cache, discovery_cache.ComposeRoundPreflightCache)
    assert rig.cache.blob_verdicts == {}
    assert declined.calls == [(1, {"reason": "mutation_during_compute"})]
    fake.during = None
    await rig.preflight()
    await rig.preflight()
    assert fake.calls == 2


_LAST_UUID = UUID(int=(1 << 128) - 1)  # sorts after every other blob id


def _add_blob(rig: _Rig, blob_id: UUID, name: str) -> None:
    storage = rig.storage.with_name(name)
    body = b"Second bound file."
    storage.write_bytes(body)
    rig.blobs.records[blob_id] = _record(blob_id, rig.session_id, storage, body)


def _marker(blob_id: UUID) -> dict[str, str]:
    return {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": "a" * 64}


async def test_a_blob_referenced_inside_a_list_is_evidence(rig: _Rig, fake: _RecordingValidatePipeline) -> None:
    _add_blob(rig, _LAST_UUID, "listed.txt")
    source = rig.state.sources["source"]
    state = replace(rig.state, sources={"source": replace(source, options={**dict(source.options), "files": [_marker(_LAST_UUID)]})})
    await rig.preflight(state)
    await rig.preflight(state)
    assert fake.calls == 1  # control: unchanged evidence reuses

    rig.blobs.records[_LAST_UUID] = replace(rig.blobs.records[_LAST_UUID], status="pending")
    await rig.preflight(state)

    assert fake.calls == 2


async def test_every_referenced_blob_is_evidence_not_only_the_first(rig: _Rig, fake: _RecordingValidatePipeline) -> None:
    _add_blob(rig, _LAST_UUID, "second.txt")
    assert sorted((rig.blob_id, _LAST_UUID))[-1] == _LAST_UUID
    source = rig.state.sources["source"]
    state = replace(rig.state, sources={"source": replace(source, options={**dict(source.options), "second": _marker(_LAST_UUID)})})
    await rig.preflight(state)

    rig.blobs.records[_LAST_UUID] = replace(rig.blobs.records[_LAST_UUID], filename="renamed.txt")
    await rig.preflight(state)

    assert fake.calls == 2


async def test_a_preflight_under_another_operation_context_recomputes(rig: _Rig, fake: _RecordingValidatePipeline) -> None:
    """The operation context is the read authority; a verdict read under one is not handed to another."""
    other = SessionOperationContext(
        fence=SessionOperationFence(str(rig.session_id), "operation-2", "lease-2", 2),
        operation_kind=SessionOperationKind.COMPOSE,
    )
    first = await rig.preflight()
    second = await rig.preflight(context=other)

    assert fake.calls == 2
    assert second != first
    assert await rig.preflight() == first
    assert await rig.preflight(context=other) == second
    assert fake.calls == 2


async def test_a_caller_that_joins_an_in_flight_preflight_does_not_overwrite_its_verdict(
    rig: _Rig, fake: _RecordingValidatePipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    reused = _reused(monkeypatch)
    declined = _declined(monkeypatch)
    gate = threading.Event()

    def hold() -> None:
        gate.wait(5)

    fake.during = hold
    owner = asyncio.create_task(rig.preflight())
    joiner = asyncio.create_task(rig.preflight())
    for _ in range(50):
        await asyncio.sleep(0)
    gate.set()
    first, second = await asyncio.gather(owner, joiner)
    fake.during = None

    assert fake.calls == 1 and second == first
    assert reused.calls == []  # the second caller joined the compute; it did not reuse a stored verdict
    assert declined.calls == [(1, {"reason": "joined_in_flight"})]
    assert await rig.preflight() == first
    assert fake.calls == 1


# F11: a verdict computed with reuse on but not kept is counted, by reason.


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        pytest.param("unlink_storage", "evidence_read_failed", id="evidence_read_failed"),
        pytest.param("no_blob_service", "no_blob_service", id="no_blob_service"),
        pytest.param("raise", "compute_failed", id="compute_failed"),
    ],
)
async def test_a_verdict_not_kept_for_reuse_is_counted_by_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setup: str, reason: str
) -> None:
    rig = _build_rig(tmp_path, with_blob_service=setup != "no_blob_service")
    fake = _RecordingValidatePipeline(raises=ValueError("synthetic") if setup == "raise" else None)
    monkeypatch.setattr(preflight_module, "validate_pipeline", fake)
    declined = _declined(monkeypatch)
    if setup == "unlink_storage":
        rig.storage.unlink()

    for _ in range(2):
        try:
            await rig.preflight()
        except ComposerRuntimePreflightError:
            assert setup == "raise"

    assert fake.calls == 2
    assert declined.calls == [(1, {"reason": reason})] * 2


async def test_a_kept_verdict_is_not_counted_as_declined(
    rig: _Rig, fake: _RecordingValidatePipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    declined = _declined(monkeypatch)
    await rig.preflight()
    await rig.preflight()
    assert declined.calls == []
