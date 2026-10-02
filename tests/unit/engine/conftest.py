# tests/unit/engine/conftest.py
"""Engine unit test fixtures."""

from typing import ClassVar

from pydantic import ConfigDict

from elspeth.contracts import PluginSchema
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.identity import TokenInfo
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.contracts.types import NodeID, StepResolver
from elspeth.core.config import CoalesceSettings
from elspeth.core.landscape.data_flow_repository import DataFlowRepository
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.engine.coalesce_executor import CoalesceExecutor
from elspeth.engine.tokens import TokenManager
from tests.fixtures.landscape import leader_token_for


class MockCoalesceExecutor(CoalesceExecutor):
    """Test wrapper that auto-provides output_schema for union merge.

    Production code computes output_schema via the DAG builder's merge_union_fields().
    Tests bypass the DAG builder, so this wrapper provides an OBSERVED-mode schema
    by default, matching the contract mode used by test fixtures.

    An OBSERVED output_schema routes _execute_merge() through the runtime
    merge_union_contracts() path (the all-OBSERVED union path), which shares
    its core algorithm with merge_union_fields().
    """

    def register_coalesce(
        self,
        settings: CoalesceSettings,
        node_id: NodeID,
        branch_schemas: dict[str, tuple[str, ...]] | None = None,
        output_schema: SchemaContract | None = None,
    ) -> None:
        if settings.on_success is None:
            settings = settings.model_copy(update={"on_success": "default"})
        if output_schema is None and settings.merge == "union":
            output_schema = SchemaContract(mode="OBSERVED", fields=(), locked=False)
        super().register_coalesce(settings, node_id, branch_schemas, output_schema)


# Dynamic schema for tests that don't care about specific fields
DYNAMIC_SCHEMA = SchemaConfig.from_dict({"mode": "observed"})


class _TestSchema(PluginSchema):
    """Dynamic schema for engine test plugins."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")


def make_test_step_resolver(step_map: dict[str, int] | None = None) -> StepResolver:
    """Create a permissive step resolver for testing.

    Returns a mapped step for known nodes, or 1 for unknown nodes. Use this
    when the test doesn't care about specific step values and just needs a
    resolver that won't crash.

    For tests that verify step values flow correctly, prefer
    make_strict_step_resolver() to catch accidentally wrong node IDs.
    """
    _map = {NodeID(k): v for k, v in (step_map or {}).items()}

    def resolve(node_id: NodeID) -> int:
        if node_id in _map:
            return _map[node_id]
        return 1  # Default step for tests

    return resolve


def make_strict_step_resolver(step_map: dict[str, int]) -> StepResolver:
    """Create a strict step resolver that crashes on unmapped node IDs.

    Mirrors production behavior (OrchestrationInvariantError on unknown nodes).
    Use this when the test exercises specific nodes and should verify the
    correct node_id is passed to the resolver.
    """
    _map = {NodeID(k): v for k, v in step_map.items()}

    def resolve(node_id: NodeID) -> int:
        if node_id not in _map:
            raise AssertionError(f"Unexpected NodeID in step resolver: {node_id!r}. Known nodes: {set(_map.keys())}")
        return _map[node_id]

    return resolve


def token_manager_leader(manager: TokenManager, run_id: str) -> CoordinationToken:
    """Read authority from a real TokenManager's durable run seat."""
    assert isinstance(manager._data_flow, DataFlowRepository)
    assert isinstance(manager._data_flow._db, LandscapeDB)
    return leader_token_for(manager._data_flow._db, run_id)


def claim_token_for_manager(manager: TokenManager, token: TokenInfo, run_id: str) -> TokenWorkItem:
    """Claim the real token on the scheduler's terminal cursor for primitive tests.

    These tests exercise TokenManager independently of node traversal. A NULL
    node cursor is a supported scheduler lane, while the claimed token, row,
    run, worker and attempt remain real persisted identities.
    """
    assert isinstance(manager._data_flow, DataFlowRepository)
    assert isinstance(manager._data_flow._db, LandscapeDB)
    leader = leader_token_for(manager._data_flow._db, run_id)
    return TokenSchedulerRepository(manager._data_flow._db.engine).enqueue_ready_claimed(
        member_token=leader.membership,
        token_id=token.token_id,
        row_id=token.row_id,
        node_id=None,
        step_index=0,
        ingest_sequence=manager._data_flow.resolve_row_ingest_sequence(token.row_id),
        row_payload_json="{}",
        lease_owner=leader.worker_id,
        lease_seconds=60,
        lineage_path=token.lineage_path,
    )
