"""AWS-boundary production sink-effect coordinator matrix (Task 9, PB-06/PB-07).

Every node crosses the production boundary: a real ``SinkExecutor`` /
``SinkEffectCoordinator`` composition writing through the real ``AWSS3Sink``
conditional-write adapter to a real S3 bucket, with all durable effect state
recorded in the lane's live AWS PostgreSQL 16 Landscape database
(``ELSPETH_STATE_ENGINE_AWS_POSTGRES_URL``), under the single-leader topology
(one coordinator identity, ``worker-a``, holds and recovers its own claims and
leases). Lane resources come only from the closed vocabulary in
``scripts/state_engine_assessment_lib/selectors.py`` (``COMMON_LIVE_RESOURCES``
plus ``AWS_RESOURCES``); missing resources skip at fixture setup so the nodes
stay collectible everywhere and executable only inside a provisioned protected
lane. The provenance assertion and the RDS-endpoint refusal mirror
``tests/e2e/recovery/test_run_lifecycle_aws_live.py``: this module never
relabels a non-AWS Landscape as the AWS deployment, and it never issues DDL
against the shared database (``create_tables=False``).

PB-07 process-loss seams are the production coordinator's own deterministic
crash seams (``SinkEffectExecutionSeam`` via the production
``sink_effect_fault_hook``), exercised in-process and recovered by a fresh
factory + fresh sink instance against the same live stores:

- ``before_reservation`` / ``after_reservation`` — PB-06
  ``reserve-pipeline-members``, PB-07 ``before-reservation``/``after-reservation``.
- ``after_preparation_claim`` — PB-06 ``claim-preparation-generation`` and
  ``death-after-preparation-claim-before-plan`` (same-leader reclaim bumps the
  generation; the interrupted claim is never reused silently).
- ``after_inspection`` — PB-06 ``inspect-or-typed-no-inspection``, PB-07
  ``after-inspection`` (the returned inspect attempt is reused, not repeated).
- ``after_plan_cas`` — PB-06 ``prepare-immutable-plan``, PB-07
  ``after-plan-cas`` (recovery restages the spool-lost body from durable
  members and commits once — PB-06 ``reconcile-not-applied-commit-once``).
- ``before_effect`` — PB-06 ``acquire-generation-fenced-lease`` plus
  ``reconcile-not-applied-commit-once``: the interrupted commit intent is
  closed as ``response_lost``, reconcile proves ``NOT_APPLIED`` against live
  S3, and exactly one physical PUT ever lands.
- ``after_effect_before_return`` — PB-07 ``after-publication-before-response``
  (response loss), PB-06 ``reconcile-applied-finalize-without-commit``: the
  object exists, reconcile returns ``APPLIED_WITH_EXACT_DESCRIPTOR`` from the
  S3 conditional-write identity metadata, and finalization performs no second
  commit (remote ETag/LastModified are byte-stable across recovery).
- ``after_return_before_finalize`` — PB-06 ``returned-result-finalization``,
  PB-07 ``after-response-before-finalization``: the durable returned commit
  attempt finalizes without any further provider call.
- ``after_finalize_before_response`` — PB-06 ``finalized-winner-reuse``, PB-07
  ``after-finalization-before-response``: the redrive loads the finalized
  winner; no new attempts, no remote mutation.

The cross-run reaffirm node covers the PB-06
``zero-publication-inherited-or-virtual`` reaffirmed arm at the AWS boundary:
a second, independent run staging byte-identical content against the same
conditional (``overwrite=False``) key finalizes ``NO_PUBLICATION`` without a
lease, commit, or reconcile call, and the remote object is untouched — the
conditional-write contract's proof of ONE effective external publication
winner across runs.

The TS-14 node covers PB-07 ``scheduler-ts14-repair-without-republication``
against the live stores: the post-sink batched scheduler callback is lost
after the effect, artifact, operation, and token outcome are durable; a fresh
processor's public resume drain terminalizes the attributed ``PENDING_SINK``
row from the terminal-outcome witness under strict leader ownership (the
TS-11 transition shape, pinned by the exact scheduler-event image) without
invoking S3 again.

Attempt-evidence tables: the final per-seam ``(action, state, generation)``
inventories are identical to the OS-kill oracle in
``tests/e2e/recovery/test_sink_effect_process_death_matrix.py``. The crash
images differ in exactly one honest way: an in-process injected fault at
``before_effect``/``after_effect_before_return`` unwinds through the
coordinator's exception arm, which durably closes the commit intent as
``response_lost`` before propagating, whereas a hard OS kill leaves the bare
``intent`` row for recovery to close. Both images converge to the same final
evidence.

Honestly scoped OUT of this module (not faked):

- A genuine OS-process kill mid-PutObject (the OS-kill matrix's
  ``after_visible_publication`` seam) cannot be scheduled deterministically
  against real AWS from an in-process pytest harness; ``after_effect_before_return``
  is the closest honest durable image (publication applied, response evidence
  lost). OS-kill seams remain owned by the process-death matrix on the local
  SQLite profiles.
- PB-06 ``reconcile-unknown-blocks``: live S3 cannot be made to yield an
  unclassifiable outcome on demand; forcing ``UNKNOWN`` requires a fake
  client, so that verdict (and its publication block) stays owned by the
  local fixture-sink matrix and the S3 unit tests.
- PB-06 ``expired-preparation-claim-takeover`` / ``expired-generation-takeover``
  / ``heartbeat-effect-lease`` contention races between two live workers:
  backend lock-order and takeover transaction semantics are proven by
  ``tests/testcontainer/core/test_sink_effect_lock_order_postgres.py``. Under
  this lane's single-leader topology recovery is the same leader identity;
  same-worker reclaim/lease-reacquisition generation discipline IS asserted
  here.
- PB-06 ``reserve-audit-export-snapshot`` (landscape-internal, no external S3
  target — ``test_audit_export_effect_recovery.py``),
  ``predecessor-wait-and-stream-advance``, ``per-member-partial-progress``,
  ``primary-diversion-failsink-ordering``, and
  ``legacy-write-flush-unreachable`` (pinned at the unit boundary: production
  ``AWSS3Sink.write`` raises unconditionally) stay with their local owners.
- TS-12 (sink-redrive leases), TS-13 bulk multi-token close, and the F-08
  partial-batch refusal arm are scheduler-repository contracts pinned by the
  scheduler unit/e2e suites; the TS-14 node here asserts the successful
  complete-membership terminalization path against the live backend.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy import select

from elspeth.contracts import NodeType, PendingOutcome, TerminalOutcome, TerminalPath, TokenInfo
from elspeth.contracts.audit import Operation, SinkEffect, SinkEffectAttempt
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.scheduler import SchedulerEventType, TokenWorkStatus
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.contracts.sink_effects import (
    SinkEffectAttemptAction,
    SinkEffectAttemptState,
    SinkEffectReconcileKind,
    SinkEffectState,
)
from elspeth.contracts.types import NodeID, SinkName
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import (
    scheduler_events_table,
    token_outcomes_table,
    token_work_items_table,
)
from elspeth.engine.executors.sink import DiversionCounts, SinkExecutor
from elspeth.engine.executors.sink_effects import SinkEffectExecutionSeam, SinkEffectInjectedFault
from elspeth.engine.orchestrator.sink_flush import SinkFlushCoordinator
from elspeth.engine.orchestrator.types import ExecutionCounters, PipelineConfig
from elspeth.engine.processor import DAGTraversalContext, RowProcessor
from elspeth.engine.spans import SpanFactory
from elspeth.plugins.aws_s3_common import build_s3_client
from elspeth.plugins.sinks.aws_s3_sink import AWSS3Sink
from tests.fixtures.base_classes import create_observed_contract, inject_write_failure
from tests.fixtures.landscape import leader_coordination_token, make_factory, register_test_node
from tests.fixtures.plugins import ListSource

if TYPE_CHECKING:
    from scripts.state_engine_profile_reporter import RuntimeProfileReporter

pytestmark = [pytest.mark.slow, pytest.mark.integration, pytest.mark.live_aws, pytest.mark.live_provider]

# The provenance assertion an operator must supply before this matrix treats
# the configured Landscape database as the live AWS deployment — the same
# constant tests/e2e/recovery/test_run_lifecycle_aws_live.py pins.
_EXPECTED_PROVENANCE = "aws-single-leader-landscape"
_ROW: dict[str, object] = {"id": 1, "value": "task9-aws-boundary"}
_WORKER_ID = "worker-a"
_SINK_NAME = "output"

# Every coordinator seam this in-process matrix can honestly reach. The two
# OS-kill-only pseudo-seams (mid-PutObject death, scheduler-callback death by
# SIGKILL) are documented as scoped out in the module docstring; scheduler
# callback LOSS is exercised by the dedicated TS-14 node below.
_COORDINATOR_SEAMS: tuple[SinkEffectExecutionSeam, ...] = (
    SinkEffectExecutionSeam.BEFORE_RESERVATION,
    SinkEffectExecutionSeam.AFTER_RESERVATION,
    SinkEffectExecutionSeam.AFTER_PREPARATION_CLAIM,
    SinkEffectExecutionSeam.AFTER_INSPECTION,
    SinkEffectExecutionSeam.AFTER_PLAN_CAS,
    SinkEffectExecutionSeam.BEFORE_EFFECT,
    SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN,
    SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE,
    SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE,
)

_Attempt = tuple[SinkEffectAttemptAction, SinkEffectAttemptState, int]
_INSPECT = SinkEffectAttemptAction.INSPECT
_RECONCILE = SinkEffectAttemptAction.RECONCILE
_COMMIT = SinkEffectAttemptAction.COMMIT
_RETURNED = SinkEffectAttemptState.RETURNED
_RESPONSE_LOST = SinkEffectAttemptState.RESPONSE_LOST

# Durable image immediately after the injected fault. Generations follow the
# repository protocol exactly: reserve=0, preparation claim +1, lease +1.
# The commit rows at before_effect/after_effect_before_return are already
# response_lost (not intent) because the in-process fault unwinds through the
# coordinator's exception arm — see the module docstring.
_CRASH_EFFECT_STATE: dict[SinkEffectExecutionSeam, SinkEffectState] = {
    SinkEffectExecutionSeam.AFTER_RESERVATION: SinkEffectState.RESERVED,
    SinkEffectExecutionSeam.AFTER_PREPARATION_CLAIM: SinkEffectState.RESERVED,
    SinkEffectExecutionSeam.AFTER_INSPECTION: SinkEffectState.RESERVED,
    SinkEffectExecutionSeam.AFTER_PLAN_CAS: SinkEffectState.PREPARED,
    SinkEffectExecutionSeam.BEFORE_EFFECT: SinkEffectState.IN_FLIGHT,
    SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN: SinkEffectState.IN_FLIGHT,
    SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE: SinkEffectState.IN_FLIGHT,
    SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE: SinkEffectState.FINALIZED,
}
_CRASH_EFFECT_GENERATION: dict[SinkEffectExecutionSeam, int] = {
    SinkEffectExecutionSeam.AFTER_RESERVATION: 0,
    SinkEffectExecutionSeam.AFTER_PREPARATION_CLAIM: 1,
    SinkEffectExecutionSeam.AFTER_INSPECTION: 1,
    SinkEffectExecutionSeam.AFTER_PLAN_CAS: 1,
    SinkEffectExecutionSeam.BEFORE_EFFECT: 2,
    SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN: 2,
    SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE: 2,
    SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE: 2,
}
_CRASH_ATTEMPTS: dict[SinkEffectExecutionSeam, tuple[_Attempt, ...]] = {
    SinkEffectExecutionSeam.AFTER_RESERVATION: (),
    SinkEffectExecutionSeam.AFTER_PREPARATION_CLAIM: (),
    SinkEffectExecutionSeam.AFTER_INSPECTION: ((_INSPECT, _RETURNED, 1),),
    SinkEffectExecutionSeam.AFTER_PLAN_CAS: ((_INSPECT, _RETURNED, 1),),
    SinkEffectExecutionSeam.BEFORE_EFFECT: (
        (_INSPECT, _RETURNED, 1),
        (_RECONCILE, _RETURNED, 2),
        (_COMMIT, _RESPONSE_LOST, 2),
    ),
    SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN: (
        (_INSPECT, _RETURNED, 1),
        (_RECONCILE, _RETURNED, 2),
        (_COMMIT, _RESPONSE_LOST, 2),
    ),
    SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE: (
        (_INSPECT, _RETURNED, 1),
        (_RECONCILE, _RETURNED, 2),
        (_COMMIT, _RETURNED, 2),
    ),
    SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE: (
        (_INSPECT, _RETURNED, 1),
        (_RECONCILE, _RETURNED, 2),
        (_COMMIT, _RETURNED, 2),
    ),
}

# Final images after recovery — identical to the OS-kill oracle tables in
# tests/e2e/recovery/test_sink_effect_process_death_matrix.py.
_FINAL_EFFECT_GENERATION: dict[SinkEffectExecutionSeam, int] = {
    SinkEffectExecutionSeam.BEFORE_RESERVATION: 2,
    SinkEffectExecutionSeam.AFTER_RESERVATION: 2,
    SinkEffectExecutionSeam.AFTER_PREPARATION_CLAIM: 3,
    SinkEffectExecutionSeam.AFTER_INSPECTION: 3,
    SinkEffectExecutionSeam.AFTER_PLAN_CAS: 2,
    SinkEffectExecutionSeam.BEFORE_EFFECT: 3,
    SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN: 3,
    SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE: 3,
    SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE: 2,
}
_FINAL_ATTEMPTS: dict[SinkEffectExecutionSeam, tuple[_Attempt, ...]] = {
    SinkEffectExecutionSeam.BEFORE_RESERVATION: (
        (_INSPECT, _RETURNED, 1),
        (_RECONCILE, _RETURNED, 2),
        (_COMMIT, _RETURNED, 2),
    ),
    SinkEffectExecutionSeam.AFTER_RESERVATION: (
        (_INSPECT, _RETURNED, 1),
        (_RECONCILE, _RETURNED, 2),
        (_COMMIT, _RETURNED, 2),
    ),
    SinkEffectExecutionSeam.AFTER_PREPARATION_CLAIM: (
        (_INSPECT, _RETURNED, 2),
        (_RECONCILE, _RETURNED, 3),
        (_COMMIT, _RETURNED, 3),
    ),
    SinkEffectExecutionSeam.AFTER_INSPECTION: (
        (_INSPECT, _RETURNED, 1),
        (_RECONCILE, _RETURNED, 3),
        (_COMMIT, _RETURNED, 3),
    ),
    SinkEffectExecutionSeam.AFTER_PLAN_CAS: (
        (_INSPECT, _RETURNED, 1),
        (_RECONCILE, _RETURNED, 2),
        (_COMMIT, _RETURNED, 2),
    ),
    SinkEffectExecutionSeam.BEFORE_EFFECT: (
        (_INSPECT, _RETURNED, 1),
        (_RECONCILE, _RETURNED, 2),
        (_COMMIT, _RESPONSE_LOST, 2),
        (_RECONCILE, _RETURNED, 3),
        (_COMMIT, _RETURNED, 3),
    ),
    SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN: (
        (_INSPECT, _RETURNED, 1),
        (_RECONCILE, _RETURNED, 2),
        (_COMMIT, _RESPONSE_LOST, 2),
        (_RECONCILE, _RETURNED, 3),
    ),
    SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE: (
        (_INSPECT, _RETURNED, 1),
        (_RECONCILE, _RETURNED, 2),
        (_COMMIT, _RETURNED, 2),
    ),
    SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE: (
        (_INSPECT, _RETURNED, 1),
        (_RECONCILE, _RETURNED, 2),
        (_COMMIT, _RETURNED, 2),
    ),
}
# The response-loss seam finalizes from reconciled evidence; every other seam
# finalizes from the returned commit result.
_FINAL_PUBLICATION_EVIDENCE: dict[SinkEffectExecutionSeam, str] = {
    seam: ("reconciled" if seam is SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN else "returned") for seam in _COORDINATOR_SEAMS
}
# Seams where the single physical PUT already landed before the crash: the
# recovered run must leave the remote object byte- and version-identical.
_PUBLISHED_AT_CRASH: frozenset[SinkEffectExecutionSeam] = frozenset(
    {
        SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN,
        SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE,
        SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE,
    }
)


@dataclass(frozen=True)
class _AwsLiveLane:
    """Resolved postgresql-16-aws composition-lane resources for this matrix."""

    database_url: str
    region: str
    bucket: str


@dataclass(frozen=True)
class _RemoteObjectImage:
    """Tier-3 admitted image of the live S3 target object."""

    etag: str
    last_modified: datetime
    size_bytes: int
    content_sha256: str | None
    effect_id: str | None


def _require_lane_env(name: str) -> str:
    """Return a required lane environment value, skipping when it is absent.

    Absent credentials make a live node honestly unexecutable — a skip, never
    a pass and never a failure. Present-but-wrong lane identity (provenance,
    endpoint) still fails loudly below.
    """
    value = os.environ.get(name)
    if value is None or not value.strip():
        pytest.skip(f"protected live-aws lane resource {name} is not set")
    return value.strip()


@pytest.fixture(scope="module")
def aws_live_lane() -> _AwsLiveLane:
    """Resolve the closed AWS lane vocabulary; skip when incomplete."""
    region = _require_lane_env("AWS_REGION")
    database_url = _require_lane_env("ELSPETH_STATE_ENGINE_AWS_POSTGRES_URL")
    provenance = _require_lane_env("ELSPETH_STATE_ENGINE_AWS_DEPLOYMENT_PROVENANCE")
    bucket = _require_lane_env("ELSPETH_TEST_S3_BUCKET")
    assert provenance == _EXPECTED_PROVENANCE
    database_host = urlparse(database_url).hostname
    assert database_host is not None
    assert database_host.endswith((".rds.amazonaws.com", ".rds.amazonaws.com.cn")), (
        "the live AWS sink-effect matrix must never relabel a non-RDS Landscape endpoint as the AWS deployment"
    )
    return _AwsLiveLane(database_url=database_url, region=region, bucket=bucket)


@pytest.fixture
def live_landscape(aws_live_lane: _AwsLiveLane) -> Iterator[LandscapeDB]:
    """Open the live AWS PostgreSQL 16 Landscape; refuse anything else."""
    with LandscapeDB.from_url(aws_live_lane.database_url, create_tables=False) as db:
        with db.read_only_connection() as connection:
            version = connection.exec_driver_sql("SHOW server_version").scalar_one()
            assert isinstance(version, str) and version.startswith("16")
        yield db


@pytest.fixture(scope="module")
def s3_client(aws_live_lane: _AwsLiveLane) -> Iterator[Any]:
    client = build_s3_client(aws_live_lane.region, None)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def s3_object_prefix(aws_live_lane: _AwsLiveLane, s3_client: Any) -> Iterator[str]:
    """Uniquely prefixed key namespace, emptied again in teardown."""
    prefix = f"elspeth-state-engine-task9/{uuid4().hex}"
    try:
        yield prefix
    finally:
        listing = s3_client.list_objects_v2(Bucket=aws_live_lane.bucket, Prefix=f"{prefix}/")
        assert isinstance(listing, dict)
        contents = listing.get("Contents", ())
        assert isinstance(contents, (list, tuple))
        for entry in contents:
            assert isinstance(entry, dict)
            entry_key = entry["Key"]
            assert isinstance(entry_key, str)
            s3_client.delete_object(Bucket=aws_live_lane.bucket, Key=entry_key)


def _head_remote_image(s3_client: Any, *, bucket: str, key: str) -> _RemoteObjectImage | None:
    """Admit the live object head, or None when the target does not exist."""
    from botocore.exceptions import ClientError  # deferred: requires the aws extra

    try:
        response = s3_client.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        error_body = error.response.get("Error")
        response_metadata = error.response.get("ResponseMetadata")
        code = error_body.get("Code") if isinstance(error_body, dict) else None
        status = response_metadata.get("HTTPStatusCode") if isinstance(response_metadata, dict) else None
        if status == 404 or code in {"404", "NotFound", "NoSuchKey"}:
            return None
        raise
    assert isinstance(response, dict)
    etag = response["ETag"]
    last_modified = response["LastModified"]
    size_bytes = response["ContentLength"]
    assert isinstance(etag, str) and etag
    assert isinstance(last_modified, datetime)
    assert type(size_bytes) is int and size_bytes >= 0
    metadata = response.get("Metadata")
    content_sha256: str | None = None
    effect_id: str | None = None
    if isinstance(metadata, dict):
        raw_hash = metadata.get("elspeth-content-sha256")
        raw_effect = metadata.get("elspeth-effect-id")
        if raw_hash is not None:
            assert isinstance(raw_hash, str)
            content_sha256 = raw_hash
        if raw_effect is not None:
            assert isinstance(raw_effect, str)
            effect_id = raw_effect
    return _RemoteObjectImage(
        etag=etag,
        last_modified=last_modified,
        size_bytes=size_bytes,
        content_sha256=content_sha256,
        effect_id=effect_id,
    )


def _read_remote_body(s3_client: Any, *, bucket: str, key: str) -> bytes:
    response = s3_client.get_object(Bucket=bucket, Key=key)
    assert isinstance(response, dict)
    body = response["Body"].read()
    assert isinstance(body, bytes)
    return body


def _begin_live_run(db: LandscapeDB) -> tuple[RecorderFactory, str, str, str]:
    """One live run with a source node and one aws_s3 sink node."""
    factory = make_factory(db)
    run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
    source_id = register_test_node(
        factory.data_flow,
        run.run_id,
        "source",
        node_type=NodeType.SOURCE,
        plugin_name="source",
    )
    sink_id = register_test_node(
        factory.data_flow,
        run.run_id,
        "primary",
        node_type=NodeType.SINK,
        plugin_name="aws_s3",
    )
    return factory, run.run_id, source_id, sink_id


def _live_tokens(
    factory: RecorderFactory,
    *,
    run_id: str,
    source_id: str,
    rows: list[dict[str, object]],
) -> list[TokenInfo]:
    tokens: list[TokenInfo] = []
    for index, row_data in enumerate(rows):
        row = factory.data_flow.create_row(
            run_id=run_id,
            source_node_id=source_id,
            row_index=index,
            data=row_data,
            source_row_index=index,
            ingest_sequence=index,
        )
        durable = factory.data_flow.create_token(row.row_id)
        tokens.append(
            TokenInfo(
                row_id=row.row_id,
                token_id=durable.token_id,
                row_data=PipelineRow(row_data, create_observed_contract(row_data)),
            )
        )
    return tokens


def _live_s3_sink(lane: _AwsLiveLane, *, key: str, node_id: str) -> AWSS3Sink:
    """A production conditional-write S3 sink bound to one exact object key."""
    sink = inject_write_failure(
        AWSS3Sink(
            {
                "bucket": lane.bucket,
                "key": key,
                "format": "jsonl",
                "overwrite": False,
                "region_name": lane.region,
                "schema": {"mode": "observed"},
            }
        )
    )
    sink.node_id = node_id
    return sink


def _pending_success() -> PendingOutcome:
    return PendingOutcome(outcome=TerminalOutcome.SUCCESS, path=TerminalPath.DEFAULT_FLOW)


def _write(
    *,
    factory: RecorderFactory,
    run_id: str,
    sink: AWSS3Sink,
    tokens: list[TokenInfo],
    fault_hook: Callable[[SinkEffectExecutionSeam], None] | None = None,
) -> tuple[Any, DiversionCounts]:
    assert sink.node_id is not None
    ctx = PluginContext(run_id=run_id, config={}, landscape=factory.plugin_audit_writer(), node_id=sink.node_id)
    return SinkExecutor(
        factory.execution,
        factory.data_flow,
        SpanFactory(),
        run_id,
        factory=factory,
        worker_id=_WORKER_ID,
        sink_effect_fault_hook=fault_hook,
    ).write(
        sink,  # type: ignore[arg-type]
        tokens,
        ctx,
        1,
        sink_name=_SINK_NAME,
        pending_outcome=_pending_success(),
        effect_mode="write",
    )


def _fail_once(seam: SinkEffectExecutionSeam) -> Callable[[SinkEffectExecutionSeam], None]:
    """Fault hook that crashes at ``seam`` on its first traversal only."""
    calls = 0

    def hook(observed: SinkEffectExecutionSeam) -> None:
        nonlocal calls
        if observed is seam and calls == 0:
            calls += 1
            raise SinkEffectInjectedFault(observed)

    return hook


def _effect_linked_operations(factory: RecorderFactory, run_id: str) -> tuple[Operation, ...]:
    return tuple(operation for operation in factory.execution.get_operations_for_run(run_id) if operation.sink_effect_id is not None)


def _assert_attempt_evidence(
    attempts: tuple[SinkEffectAttempt, ...],
    *,
    effect: SinkEffect,
    expected: tuple[_Attempt, ...],
) -> None:
    """Exact attempt inventory plus per-state durable evidence discipline."""
    assert all(attempt.effect_id == effect.effect_id for attempt in attempts)
    assert all(0 <= attempt.generation <= effect.generation for attempt in attempts)
    for attempt in attempts:
        if attempt.state is SinkEffectAttemptState.RETURNED:
            assert attempt.evidence_json is not None
            assert attempt.evidence_hash is not None
            assert attempt.completed_at is not None
            assert attempt.latency_ms is not None
        elif attempt.state is SinkEffectAttemptState.RESPONSE_LOST:
            assert attempt.evidence_json == '{"classification":"response_lost"}'
            assert attempt.evidence_hash is not None
            assert attempt.completed_at is not None
        else:
            assert attempt.state is SinkEffectAttemptState.INTENT
            assert attempt.evidence_json is None
            assert attempt.evidence_hash is None
            assert attempt.completed_at is None
            assert attempt.latency_ms is None
    assert Counter((attempt.action, attempt.state, attempt.generation) for attempt in attempts) == Counter(expected)


def test_postgresql16_aws_boundary_profile_binds(
    live_landscape: LandscapeDB,
    request: pytest.FixtureRequest,
) -> None:
    """Bind the profile evidence to the real AWS PostgreSQL 16 Landscape.

    Exactly one node per evidence run may observe the execution profile
    (``RuntimeProfileReporter`` rejects divergent observations), so this
    dedicated node carries the ``observe_postgresql`` binding for the whole
    module while the fixture-level version guard protects every node.
    """
    with live_landscape.read_only_connection() as connection:
        version = connection.exec_driver_sql("SHOW server_version").scalar_one()
        assert isinstance(version, str) and version.startswith("16")
        if request.config.pluginmanager.hasplugin("scripts.state_engine_profile_reporter"):
            reporter = cast("RuntimeProfileReporter", request.getfixturevalue("state_engine_profile"))
            reporter.observe_postgresql(connection, deployment=_EXPECTED_PROVENANCE)


@pytest.mark.parametrize("seam", _COORDINATOR_SEAMS, ids=[seam.value for seam in _COORDINATOR_SEAMS])
def test_coordinator_seam_crash_recovers_to_one_effective_s3_publication(
    live_landscape: LandscapeDB,
    aws_live_lane: _AwsLiveLane,
    s3_client: Any,
    s3_object_prefix: str,
    seam: SinkEffectExecutionSeam,
) -> None:
    """PB-06/PB-07: every coordinator seam converges to one effect ID, exact
    attempt evidence, generation discipline, and ONE effective S3 publication
    proven through the conditional-write contract and remote-image identity."""
    factory, run_id, source_id, sink_id = _begin_live_run(live_landscape)
    key = f"{s3_object_prefix}/{seam.value}/output.jsonl"
    (token,) = _live_tokens(factory, run_id=run_id, source_id=source_id, rows=[dict(_ROW)])

    first_sink = _live_s3_sink(aws_live_lane, key=key, node_id=sink_id)
    try:
        with pytest.raises(SinkEffectInjectedFault):
            _write(factory=factory, run_id=run_id, sink=first_sink, tokens=[token], fault_hook=_fail_once(seam))
    finally:
        first_sink.close()

    # --- Complete durable crash image (Landscape) -------------------------
    crash_effects = factory.execution.sink_effects.get_effects_for_run(run_id)
    crash_effect_id: str | None = None
    crash_operation_id: str | None = None
    if seam is SinkEffectExecutionSeam.BEFORE_RESERVATION:
        assert crash_effects == ()
        assert _effect_linked_operations(factory, run_id) == ()
    else:
        (crash_effect,) = crash_effects
        crash_effect_id = crash_effect.effect_id
        assert crash_effect.state is _CRASH_EFFECT_STATE[seam]
        assert crash_effect.generation == _CRASH_EFFECT_GENERATION[seam]
        _assert_attempt_evidence(
            factory.execution.sink_effects.get_attempts(crash_effect.effect_id),
            effect=crash_effect,
            expected=_CRASH_ATTEMPTS[seam],
        )
        (crash_operation,) = _effect_linked_operations(factory, run_id)
        crash_operation_id = crash_operation.operation_id
        assert crash_operation.sink_effect_id == crash_effect.effect_id
        expected_operation_status = "completed" if seam is SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE else "open"
        assert crash_operation.status == expected_operation_status
    crash_outcome = factory.data_flow.get_token_outcome(token.token_id)
    crash_artifacts = factory.execution.get_artifacts(run_id)
    if seam is SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE:
        assert crash_outcome is not None
        assert len(crash_artifacts) == 1
    else:
        assert crash_outcome is None
        assert len(crash_artifacts) == 0

    # --- External target crash image (live S3) ----------------------------
    crash_remote = _head_remote_image(s3_client, bucket=aws_live_lane.bucket, key=key)
    if seam in _PUBLISHED_AT_CRASH:
        assert crash_remote is not None
        assert crash_remote.effect_id == crash_effect_id
    else:
        # Zero external mutation before the commit call: reservation,
        # preparation claim, inspection, and plan CAS never touch the target.
        assert crash_remote is None

    # --- Recovery: fresh factory, fresh sink, same leader identity --------
    recovered_factory = make_factory(live_landscape)
    recovered_sink = _live_s3_sink(aws_live_lane, key=key, node_id=sink_id)
    try:
        artifact, counts = _write(factory=recovered_factory, run_id=run_id, sink=recovered_sink, tokens=[token])
    finally:
        recovered_sink.close()

    assert artifact is not None
    assert counts == DiversionCounts()

    (final_effect,) = recovered_factory.execution.sink_effects.get_effects_for_run(run_id)
    if crash_effect_id is not None:
        assert final_effect.effect_id == crash_effect_id  # one effect ID, ever
    assert final_effect.state is SinkEffectState.FINALIZED
    assert final_effect.generation == _FINAL_EFFECT_GENERATION[seam]
    assert final_effect.publication_performed is True
    assert final_effect.publication_evidence_kind == _FINAL_PUBLICATION_EVIDENCE[seam]
    if seam is SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN:
        assert final_effect.reconcile_kind is SinkEffectReconcileKind.APPLIED_WITH_EXACT_DESCRIPTOR
    else:
        assert final_effect.reconcile_kind is None
    _assert_attempt_evidence(
        recovered_factory.execution.sink_effects.get_attempts(final_effect.effect_id),
        effect=final_effect,
        expected=_FINAL_ATTEMPTS[seam],
    )
    members = recovered_factory.execution.sink_effects.get_members(final_effect.effect_id)
    assert [(member.ordinal, member.prepared_disposition) for member in members] == [(0, "accepted")]

    (final_artifact,) = recovered_factory.execution.get_artifacts(run_id)
    assert artifact.artifact_id == final_artifact.artifact_id
    assert final_artifact.sink_effect_id == final_effect.effect_id
    assert final_artifact.path_or_uri == f"s3://{aws_live_lane.bucket}/{key}"
    (final_operation,) = _effect_linked_operations(recovered_factory, run_id)
    assert final_operation.status == "completed"
    assert final_operation.sink_effect_id == final_effect.effect_id
    if crash_operation_id is not None:
        assert final_operation.operation_id == crash_operation_id
    outcome = recovered_factory.data_flow.get_token_outcome(token.token_id)
    assert outcome is not None
    assert (outcome.outcome, outcome.path, outcome.sink_name) == (
        TerminalOutcome.SUCCESS,
        TerminalPath.DEFAULT_FLOW,
        _SINK_NAME,
    )

    # --- External target final image: ONE effective publication -----------
    final_remote = _head_remote_image(s3_client, bucket=aws_live_lane.bucket, key=key)
    assert final_remote is not None
    assert final_remote.effect_id == final_effect.effect_id
    body = _read_remote_body(s3_client, bucket=aws_live_lane.bucket, key=key)
    assert [json.loads(line) for line in body.decode("utf-8").splitlines()] == [_ROW]
    assert sha256(body).hexdigest() == final_artifact.content_hash
    assert final_remote.content_sha256 == final_artifact.content_hash
    assert len(body) == final_artifact.size_bytes == final_remote.size_bytes
    if crash_remote is not None:
        # The already-published object was never re-put: the exact-attempt
        # inventory above pins the provider request count, and the remote
        # version identity corroborates it.
        assert (final_remote.etag, final_remote.last_modified, final_remote.size_bytes) == (
            crash_remote.etag,
            crash_remote.last_modified,
            crash_remote.size_bytes,
        )


def test_second_run_identical_content_reaffirms_without_republication(
    live_landscape: LandscapeDB,
    aws_live_lane: _AwsLiveLane,
    s3_client: Any,
    s3_object_prefix: str,
) -> None:
    """PB-06 zero-publication (reaffirmed arm) at the AWS conditional-write
    boundary: an independent second run staging byte-identical content against
    the same ``overwrite=False`` key finalizes NO_PUBLICATION — no lease, no
    commit, no reconcile — and the remote winner is untouched."""
    key = f"{s3_object_prefix}/reaffirm/output.jsonl"

    factory_a, run_a, source_a, sink_a_id = _begin_live_run(live_landscape)
    (token_a,) = _live_tokens(factory_a, run_id=run_a, source_id=source_a, rows=[dict(_ROW)])
    sink_a = _live_s3_sink(aws_live_lane, key=key, node_id=sink_a_id)
    try:
        artifact_a, counts_a = _write(factory=factory_a, run_id=run_a, sink=sink_a, tokens=[token_a])
    finally:
        sink_a.close()
    assert artifact_a is not None
    assert counts_a == DiversionCounts()
    (effect_a,) = factory_a.execution.sink_effects.get_effects_for_run(run_a)
    assert effect_a.state is SinkEffectState.FINALIZED
    assert effect_a.generation == 2
    assert effect_a.publication_performed is True
    _assert_attempt_evidence(
        factory_a.execution.sink_effects.get_attempts(effect_a.effect_id),
        effect=effect_a,
        expected=((_INSPECT, _RETURNED, 1), (_RECONCILE, _RETURNED, 2), (_COMMIT, _RETURNED, 2)),
    )
    remote_after_a = _head_remote_image(s3_client, bucket=aws_live_lane.bucket, key=key)
    assert remote_after_a is not None
    assert remote_after_a.effect_id == effect_a.effect_id

    factory_b, run_b, source_b, sink_b_id = _begin_live_run(live_landscape)
    (token_b,) = _live_tokens(factory_b, run_id=run_b, source_id=source_b, rows=[dict(_ROW)])
    sink_b = _live_s3_sink(aws_live_lane, key=key, node_id=sink_b_id)
    try:
        artifact_b, counts_b = _write(factory=factory_b, run_id=run_b, sink=sink_b, tokens=[token_b])
    finally:
        sink_b.close()

    assert artifact_b is not None
    assert counts_b == DiversionCounts()
    assert artifact_b.publication_performed is False
    # The Artifact row deliberately carries only the coarse "inherited"
    # evidence kind; the finer reaffirmed distinction survives in the
    # finalized effect's persisted plan (see the local reaffirm proof).
    assert artifact_b.publication_evidence_kind == "inherited"
    assert artifact_b.content_hash == artifact_a.content_hash
    (effect_b,) = factory_b.execution.sink_effects.get_effects_for_run(run_b)
    assert effect_b.effect_id != effect_a.effect_id
    assert effect_b.state is SinkEffectState.FINALIZED
    assert effect_b.generation == 1
    assert effect_b.publication_performed is False
    assert effect_b.publication_evidence_kind == "inherited"
    assert effect_b.predecessor_effect_id is None
    assert effect_b.plan_json is not None
    persisted_plan = json.loads(effect_b.plan_json)
    assert persisted_plan["safe_evidence"]["publication_kind"] == "reaffirmed"
    # No lease, no commit, no reconcile: the only provider call is inspection.
    _assert_attempt_evidence(
        factory_b.execution.sink_effects.get_attempts(effect_b.effect_id),
        effect=effect_b,
        expected=((_INSPECT, _RETURNED, 1),),
    )
    outcome_b = factory_b.data_flow.get_token_outcome(token_b.token_id)
    assert outcome_b is not None
    assert (outcome_b.outcome, outcome_b.path, outcome_b.sink_name) == (
        TerminalOutcome.SUCCESS,
        TerminalPath.DEFAULT_FLOW,
        _SINK_NAME,
    )

    remote_after_b = _head_remote_image(s3_client, bucket=aws_live_lane.bucket, key=key)
    assert remote_after_b is not None
    assert (remote_after_b.etag, remote_after_b.last_modified, remote_after_b.size_bytes) == (
        remote_after_a.etag,
        remote_after_a.last_modified,
        remote_after_a.size_bytes,
    )
    assert remote_after_b.effect_id == effect_a.effect_id


def test_ts14_scheduler_callback_loss_repairs_without_republishing_s3_effect(
    live_landscape: LandscapeDB,
    aws_live_lane: _AwsLiveLane,
    s3_client: Any,
    s3_object_prefix: str,
) -> None:
    """PB-07 scheduler-ts14-repair-without-republication against live stores.

    The batched post-sink scheduler callback is lost only after the S3 effect,
    artifact, operation, and token outcome are durable. A fresh processor's
    public resume drain must terminalize the attributed PENDING_SINK row from
    the terminal-outcome witness (TS-11 transition shape, exact event image)
    without a second S3 request.
    """

    class _LostSchedulerTerminalizer:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def mark_sink_bound_scheduler_terminal_many(self, token_ids: tuple[str, ...]) -> None:
            self.calls.append(token_ids)
            raise RuntimeError("injected loss before scheduler terminalization")

    factory, run_id, source_id, sink_id = _begin_live_run(live_landscape)
    key = f"{s3_object_prefix}/ts14-callback-loss/output.jsonl"
    (token,) = _live_tokens(factory, run_id=run_id, source_id=source_id, rows=[dict(_ROW)])
    ctx = PluginContext(run_id=run_id, config={}, landscape=factory.plugin_audit_writer(), node_id=sink_id)
    leader = leader_coordination_token(factory, run_id)
    now = datetime.now(UTC)
    ready = factory.scheduler.enqueue_ready(
        run_id=run_id,
        token_id=token.token_id,
        row_id=token.row_id,
        node_id=sink_id,
        step_index=1,
        ingest_sequence=0,
        row_payload_json=factory.scheduler.serialize_row_payload(token.row_data),
        available_at=now,
    )
    claimed = factory.scheduler.claim_ready(
        run_id=run_id,
        lease_owner=leader.worker_id,
        lease_seconds=300,
        now=now,
    )
    assert claimed is not None and claimed.work_item_id == ready.work_item_id
    parked = factory.scheduler.mark_pending_sink(
        work_item_id=claimed.work_item_id,
        row_payload_json=factory.scheduler.serialize_row_payload(token.row_data),
        sink_name=_SINK_NAME,
        outcome=TerminalOutcome.SUCCESS.value,
        path=TerminalPath.DEFAULT_FLOW.value,
        error_hash=None,
        error_message=None,
        now=now,
        expected_lease_owner=leader.worker_id,
        worker_id=leader.worker_id,
    )
    assert parked.status is TokenWorkStatus.PENDING_SINK

    sink = _live_s3_sink(aws_live_lane, key=key, node_id=sink_id)
    pending_outcome = PendingOutcome(
        outcome=TerminalOutcome.SUCCESS,
        path=TerminalPath.DEFAULT_FLOW,
        scheduler_pending_sink=True,
    )
    pending_tokens = {_SINK_NAME: [(token, pending_outcome)]}
    lost_terminalizer = _LostSchedulerTerminalizer()
    coordinator = SinkFlushCoordinator(span_factory=SpanFactory(), checkpoints=object())  # type: ignore[arg-type]
    config = PipelineConfig(
        sources={"source": ListSource([])},
        transforms=(),
        sinks={_SINK_NAME: sink},  # type: ignore[dict-item]
        sink_effect_modes={_SINK_NAME: "write"},
    )

    try:
        with pytest.raises(RuntimeError, match="injected loss before scheduler terminalization"):
            coordinator.write_pending_to_sinks(
                factory=factory,
                run_id=run_id,
                config=config,
                ctx=ctx,
                counters=ExecutionCounters(),
                pending_tokens=pending_tokens,
                sink_id_map={SinkName(_SINK_NAME): NodeID(sink_id)},
                edge_map={},
                sink_step=1,
                scheduler_terminalizer=lost_terminalizer,
                worker_id=leader.worker_id,
            )
    finally:
        sink.close()

    assert lost_terminalizer.calls == [(token.token_id,)]

    # Durable image at callback loss: the effect side is complete.
    (effect_before,) = factory.execution.sink_effects.get_effects_for_run(run_id)
    (artifact_before,) = factory.execution.get_artifacts(run_id)
    (operation_before,) = _effect_linked_operations(factory, run_id)
    assert effect_before.state is SinkEffectState.FINALIZED
    assert artifact_before.sink_effect_id == effect_before.effect_id
    assert operation_before.sink_effect_id == effect_before.effect_id
    assert operation_before.status == "completed"
    attempts_before = factory.execution.sink_effects.get_attempts(effect_before.effect_id)
    outcome_before = factory.data_flow.get_token_outcome(token.token_id)
    assert outcome_before is not None
    assert (outcome_before.outcome, outcome_before.path, outcome_before.sink_name) == (
        TerminalOutcome.SUCCESS,
        TerminalPath.DEFAULT_FLOW,
        _SINK_NAME,
    )
    remote_before = _head_remote_image(s3_client, bucket=aws_live_lane.bucket, key=key)
    assert remote_before is not None
    assert remote_before.effect_id == effect_before.effect_id
    with live_landscape.connection() as conn:
        callback_loss_image = conn.execute(
            select(
                token_work_items_table.c.work_item_id,
                token_work_items_table.c.status,
                token_work_items_table.c.lease_owner,
                token_work_items_table.c.pending_sink_name,
                token_work_items_table.c.pending_outcome,
                token_work_items_table.c.pending_path,
            ).where(token_work_items_table.c.token_id == token.token_id)
        ).one()
    assert tuple(callback_loss_image) == (
        parked.work_item_id,
        TokenWorkStatus.PENDING_SINK.value,
        leader.worker_id,
        _SINK_NAME,
        TerminalOutcome.SUCCESS.value,
        TerminalPath.DEFAULT_FLOW.value,
    )

    # Public resume drain from a fresh factory repairs the scheduler debt.
    recovered_factory = make_factory(live_landscape)
    source_node_id = NodeID(source_id)
    traversal = DAGTraversalContext(
        node_step_map={source_node_id: 0},
        node_to_plugin={},
        node_to_next={source_node_id: None},
        coalesce_node_map={},
        structural_node_ids=frozenset({source_node_id}),
    )
    resumed = RowProcessor(
        execution=recovered_factory.execution,
        data_flow=recovered_factory.data_flow,
        span_factory=SpanFactory(),
        run_id=run_id,
        source_node_id=source_node_id,
        source_on_success=_SINK_NAME,
        traversal=traversal,
        scheduler=recovered_factory.scheduler,
        scheduler_lease_owner=leader.worker_id,
        coordination_token=leader,
    )
    assert resumed.drain_scheduled_work(ctx) == []

    # Repair converges without republication: same effect, artifact,
    # operation, outcome, attempts — and the remote object is untouched.
    (effect_after,) = recovered_factory.execution.sink_effects.get_effects_for_run(run_id)
    (artifact_after,) = recovered_factory.execution.get_artifacts(run_id)
    (operation_after,) = _effect_linked_operations(recovered_factory, run_id)
    assert effect_after.effect_id == effect_before.effect_id
    assert artifact_after.artifact_id == artifact_before.artifact_id
    assert operation_after.operation_id == operation_before.operation_id
    attempts_after = recovered_factory.execution.sink_effects.get_attempts(effect_after.effect_id)
    assert [(a.attempt_id, a.state) for a in attempts_after] == [(a.attempt_id, a.state) for a in attempts_before]
    assert recovered_factory.data_flow.get_token_outcome(token.token_id) == outcome_before
    remote_after = _head_remote_image(s3_client, bucket=aws_live_lane.bucket, key=key)
    assert remote_after is not None
    assert (remote_after.etag, remote_after.last_modified, remote_after.size_bytes) == (
        remote_before.etag,
        remote_before.last_modified,
        remote_before.size_bytes,
    )

    with live_landscape.connection() as conn:
        work_after = conn.execute(
            select(
                token_work_items_table.c.work_item_id,
                token_work_items_table.c.status,
                token_work_items_table.c.lease_owner,
                token_work_items_table.c.pending_sink_name,
                token_work_items_table.c.pending_outcome,
                token_work_items_table.c.pending_path,
            ).where(token_work_items_table.c.token_id == token.token_id)
        ).one()
        outcome_rows = conn.execute(
            select(
                token_outcomes_table.c.token_id,
                token_outcomes_table.c.outcome,
                token_outcomes_table.c.path,
                token_outcomes_table.c.completed,
                token_outcomes_table.c.sink_name,
            ).where(token_outcomes_table.c.token_id == token.token_id)
        ).all()
        scheduler_events = conn.execute(
            select(
                scheduler_events_table.c.event_type,
                scheduler_events_table.c.from_status,
                scheduler_events_table.c.to_status,
                scheduler_events_table.c.from_lease_owner,
                scheduler_events_table.c.to_lease_owner,
                scheduler_events_table.c.caller_owner,
            ).where(scheduler_events_table.c.token_id == token.token_id)
        ).all()

    assert tuple(work_after) == (
        parked.work_item_id,
        TokenWorkStatus.TERMINAL.value,
        None,
        _SINK_NAME,
        TerminalOutcome.SUCCESS.value,
        TerminalPath.DEFAULT_FLOW.value,
    )
    assert [tuple(row) for row in outcome_rows] == [
        (
            token.token_id,
            TerminalOutcome.SUCCESS.value,
            TerminalPath.DEFAULT_FLOW.value,
            True,
            _SINK_NAME,
        )
    ]
    event_image = {row.event_type: tuple(row)[1:] for row in scheduler_events}
    assert len(event_image) == len(scheduler_events) == 4
    assert event_image == {
        SchedulerEventType.ENQUEUE.value: (None, TokenWorkStatus.READY.value, None, None, None),
        SchedulerEventType.CLAIM_READY.value: (
            TokenWorkStatus.READY.value,
            TokenWorkStatus.LEASED.value,
            None,
            leader.worker_id,
            leader.worker_id,
        ),
        SchedulerEventType.MARK_PENDING_SINK.value: (
            TokenWorkStatus.LEASED.value,
            TokenWorkStatus.PENDING_SINK.value,
            leader.worker_id,
            leader.worker_id,
            leader.worker_id,
        ),
        SchedulerEventType.MARK_PENDING_SINK_TERMINAL.value: (
            TokenWorkStatus.PENDING_SINK.value,
            TokenWorkStatus.TERMINAL.value,
            leader.worker_id,
            None,
            leader.worker_id,
        ),
    }
