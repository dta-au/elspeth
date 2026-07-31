"""Fork-plan custody contracts for source blob paths."""

from __future__ import annotations

import ast
import inspect
from uuid import UUID

from elspeth.contracts.blobs import BlobForkPlanEntry, fork_blob_id
from elspeth.web.sessions import service as session_service
from elspeth.web.sessions.routes import sessions as session_routes


def test_fork_blob_plan_round_trip_retains_frozen_source_storage_path() -> None:
    source_session_id = UUID("11111111-1111-4111-8111-111111111111")
    child_session_id = UUID("22222222-2222-4222-8222-222222222222")
    source_blob_id = UUID("33333333-3333-4333-8333-333333333333")
    entry = BlobForkPlanEntry(
        source_blob_id=source_blob_id,
        target_blob_id=fork_blob_id(
            target_session_id=child_session_id,
            source_blob_id=source_blob_id,
        ),
        source_storage_path=f"/data/blobs/{source_session_id}/{source_blob_id}_source.csv",
        content_hash="a" * 64,
        size_bytes=17,
    )

    content = session_service._fork_blob_plan_content(
        source_session_id=source_session_id,
        child_session_id=child_session_id,
        operation_id="fork-operation",
        entries=(entry,),
    )

    assert session_service._fork_blob_plan_from_content(
        content,
        expected_source_session_id=source_session_id,
        expected_child_session_id=child_session_id,
        expected_operation_id="fork-operation",
    ) == (entry,)


def test_fork_route_uses_composite_frozen_plan_without_generic_blob_read() -> None:
    tree = ast.parse(inspect.getsource(session_routes))
    endpoints = [node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "fork_from_message"]
    assert len(endpoints) == 1
    endpoint = endpoints[0]
    generic_reads = [
        node
        for node in ast.walk(endpoint)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get_blob"
    ]
    assert generic_reads == []
    assert any(
        isinstance(node, ast.Attribute)
        and node.attr == "source_storage_path"
        and isinstance(node.value, ast.Name)
        and node.value.id == "entry"
        for node in ast.walk(endpoint)
    )
