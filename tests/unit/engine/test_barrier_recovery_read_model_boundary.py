from __future__ import annotations

import ast
from pathlib import Path

RESTORE_READ_METHODS = {
    "find_duplicate_live_buffered_outcomes",
    "get_failed_unrouted_terminal_token_ids",
    "get_live_buffered_outcomes",
}
RESTORE_NODE_STATE_READ_METHODS = {
    "find_released_node_state_token_ids",
    "get_completed_row_ids_for_nodes",
    "get_completed_group_ids_for_nodes",  # WS4 T2: group-keyed sibling of get_completed_row_ids_for_nodes
    "get_max_node_state_attempts",
    "get_max_node_state_attempts_for_node",  # WS4 T7: node-scoped sibling (META-14.1 opener attempt)
    "get_open_node_state_ids",
    "get_released_row_ids_for_nodes",
    "get_released_group_ids_for_nodes",  # WS4 T12/F-1: group-keyed sibling of get_released_row_ids_for_nodes
    "get_settled_member_token_ids",  # WS4 T7: settled-token twin of has_completed_group_for_node (META-20b)
    "resolve_group_collector_node",  # WS4 T7: durable node-resolution family anchor (META-22)
    "has_group_loss",  # WS4 T12: renamed from has_branch_loss_for_group -- queries group_losses directly by group_id
    "has_completed_row_for_node",
    "has_completed_group_for_node",  # WS4 T2: group-keyed sibling of has_completed_row_for_node
    "has_released_row_for_node",
    "has_released_group_for_node",  # WS4 T2: group-keyed sibling of has_released_row_for_node
}

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_barrier_recovery_uses_restore_read_model_for_token_outcome_policy() -> None:
    source_path = REPO_ROOT / "src/elspeth/engine/barrier_coordination.py"
    tree = ast.parse(source_path.read_text(), filename=str(source_path))
    recovery = next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "BarrierRecoveryCoordinator")

    data_flow_restore_reads: list[str] = []
    for node in ast.walk(recovery):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in RESTORE_READ_METHODS:
            continue
        receiver = func.value
        if isinstance(receiver, ast.Attribute) and receiver.attr == "_data_flow":
            data_flow_restore_reads.append(func.attr)

    assert data_flow_restore_reads == []


def test_restore_and_coalesce_node_state_reads_do_not_use_execution_facade() -> None:
    checked = [
        (
            REPO_ROOT / "src/elspeth/engine/barrier_coordination.py",
            {"BarrierIntakeCoordinator", "BarrierRecoveryCoordinator"},
        ),
        (
            REPO_ROOT / "src/elspeth/engine/scheduler_drain.py",
            {"SchedulerDrainCoordinator"},
        ),
        (
            REPO_ROOT / "src/elspeth/engine/coalesce_executor.py",
            {"CoalesceExecutor"},
        ),
        (
            REPO_ROOT / "src/elspeth/engine/journal_restore.py",
            {"CoalesceJournalRestorer", "CollectorJournalRestorer"},  # WS4 T7
        ),
        (
            REPO_ROOT / "src/elspeth/engine/row_union_executor.py",
            {"RowUnionExecutor"},
        ),
        (
            REPO_ROOT / "src/elspeth/engine/executors/collector.py",
            {"CollectorExecutor"},  # WS4 T7
        ),
    ]
    execution_restore_reads: list[tuple[str, str, str]] = []

    for source_path, class_names in checked:
        tree = ast.parse(source_path.read_text(), filename=str(source_path))
        for class_node in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name in class_names):
            for call_node in ast.walk(class_node):
                if not isinstance(call_node, ast.Call):
                    continue
                func = call_node.func
                if not isinstance(func, ast.Attribute) or func.attr not in RESTORE_NODE_STATE_READ_METHODS:
                    continue
                receiver = func.value
                if isinstance(receiver, ast.Attribute) and receiver.attr == "_execution":
                    execution_restore_reads.append((class_node.name, func.attr, source_path.name))

    assert execution_restore_reads == []
