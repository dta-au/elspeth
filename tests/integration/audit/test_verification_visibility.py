"""Persisted verification decisions are visible through CLI and read-only MCP."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import update
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts import CallStatus, CallType, NodeType
from elspeth.contracts.audit import CallVerification
from elspeth.contracts.call_data import RawCallPayload
from elspeth.contracts.enums import RunMode
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import call_verifications_table
from elspeth.mcp.analyzer import LandscapeAnalyzer
from elspeth.mcp.server import _TOOLS, _validate_tool_args
from elspeth.tui.screens.explain_screen import ExplainScreen
from tests.fixtures.landscape import (
    claim_test_work_item,
    leader_coordination_token,
    leader_member_token,
    make_recorder_with_run,
    register_test_node,
)


def _persist_verdict(db_path: Path, *, is_match: bool, operation_parent: bool = False) -> tuple[str, CallVerification]:
    db = LandscapeDB.from_url(f"sqlite:///{db_path}")
    try:
        setup = make_recorder_with_run(db=db, run_id="source", source_node_id="source-node")
        factory = setup.factory
        factory.run_lifecycle.begin_run(
            config={}, canonical_version="v1", run_id="current", run_mode=RunMode.VERIFY, replay_from_run_id="source"
        )
        register_test_node(factory.data_flow, "current", "source-node", node_type=NodeType.SOURCE)
        calls = []
        current_token_id = ""
        for run_id in ("source", "current"):
            leader = leader_coordination_token(factory, run_id)
            response = {"status": 200 if run_id == "source" or is_match else 201}
            if operation_parent:
                operation = factory.execution.begin_operation("source-node", "source_load", coordination_token=leader)
                call = factory.execution.record_operation_call(
                    operation.operation_id,
                    CallType.HTTP,
                    CallStatus.SUCCESS,
                    request_data=RawCallPayload({"url": "https://example.test"}),
                    response_data=RawCallPayload(response),
                    coordination_token=leader,
                )
            else:
                register_test_node(factory.data_flow, run_id, "transform")
                _, token = factory.data_flow.create_row_with_token(
                    coordination_token=leader,
                    source_node_id="source-node",
                    row_index=0,
                    data={"value": 1},
                    source_row_index=0,
                    ingest_sequence=0,
                )
                member = leader_member_token(factory, run_id)
                work = claim_test_work_item(factory, member_token=member, token_id=token.token_id, node_id="transform")
                state = factory.execution.begin_node_state(token.token_id, "transform", 0, {"value": 1}, member_token=member)
                call = factory.execution.record_call(
                    state.state_id,
                    0,
                    CallType.HTTP,
                    CallStatus.SUCCESS,
                    request_data=RawCallPayload({"url": "https://example.test"}),
                    response_data=RawCallPayload(response),
                    member_token=member,
                    work_item=work,
                )
                current_token_id = token.token_id
            calls.append(call)
        differences = (
            {}
            if is_match
            else {
                "reason": "response_hash_mismatch",
                "expected_hash": calls[0].response_hash,
                "actual_hash": calls[1].response_hash,
            }
        )
        decision = factory.execution.record_verification_decision(
            current_run_id="current",
            current_call_id=calls[1].call_id,
            source_run_id="source",
            source_call_id=calls[0].call_id,
            is_match=is_match,
            differences_json=json.dumps(differences),
            coordination_token=leader_coordination_token(factory, "current"),
        )
        return current_token_id, decision
    finally:
        db.close()


@pytest.mark.parametrize("is_match", [True, False])
@pytest.mark.parametrize("operation_parent", [True, False])
def test_read_only_mcp_tool_returns_persisted_verdicts(tmp_path: Path, is_match: bool, operation_parent: bool) -> None:
    db_path = tmp_path / "audit.db"
    token_id, decision = _persist_verdict(db_path, is_match=is_match, operation_parent=operation_parent)
    analyzer = LandscapeAnalyzer(f"sqlite:///{db_path}")
    try:
        args = _validate_tool_args("list_verification_decisions", {"run_id": "current"})
        records = _TOOLS["list_verification_decisions"].handler(analyzer, args)
        assert len(records) == 1
        assert records[0]["is_match"] is is_match
        assert records[0]["current_call_id"] == decision.current_call_id
        assert records[0]["source_call_id"] == decision.source_call_id
        assert records[0]["current_run_id"] == "current"
        assert records[0]["source_run_id"] == "source"
        assert records[0]["differences_json"] == decision.differences_json
        assert records[0]["recorded_at"]
        assert analyzer.list_verification_decisions("source") == []
        if not operation_parent:
            result = analyzer.explain_token("current", token_id=token_id)
            assert "error" not in result
            assert result["verification_decisions"] == records
    finally:
        analyzer.close()


@pytest.mark.parametrize("is_match", [True, False])
@pytest.mark.parametrize("output_flag", ["--json", "--no-tui"])
def test_explain_cli_shows_persisted_verdict(tmp_path: Path, is_match: bool, output_flag: str) -> None:
    db_path = tmp_path / "audit.db"
    token_id, decision = _persist_verdict(db_path, is_match=is_match)
    result = CliRunner().invoke(app, ["explain", "--run", "current", "--token", token_id, "--database", str(db_path), output_flag])
    assert result.exit_code == 0, result.output
    if output_flag == "--json":
        records = json.loads(result.output)["verification_decisions"]
        assert len(records) == 1
        assert records[0]["is_match"] is is_match
        assert records[0]["differences_json"] == decision.differences_json
        assert records[0]["source_call_id"] == decision.source_call_id
    else:
        assert "Verification decisions" in result.output
        assert ("MATCH" if is_match else "MISMATCH") in result.output
        assert decision.current_call_id in result.output
        assert decision.source_call_id in result.output
        assert decision.differences_json in result.output


@pytest.mark.parametrize("is_match", [True, False])
@pytest.mark.parametrize("operation_parent", [True, False])
def test_explain_tui_run_summary_shows_persisted_verdict(tmp_path: Path, is_match: bool, operation_parent: bool) -> None:
    db_path = tmp_path / "audit.db"
    _, decision = _persist_verdict(db_path, is_match=is_match, operation_parent=operation_parent)
    db = LandscapeDB.from_url(f"sqlite:///{db_path}", create_tables=False, read_only=True)
    try:
        screen = ExplainScreen(db=db, run_id="current")
        screen.on_tree_select({"kind": "run", "run_id": "current"})
        content = screen.detail_panel.render_content()
        assert "Verification decisions" in content
        assert ("MATCH" if is_match else "MISMATCH") in content
        assert decision.current_call_id in content
        assert decision.source_call_id in content
        assert decision.differences_json in content
    finally:
        db.close()


@pytest.mark.parametrize("output_flag", ["--json", "--no-tui"])
def test_explain_cli_propagates_corrupt_verification(tmp_path: Path, output_flag: str) -> None:
    db_path = tmp_path / "audit.db"
    token_id, decision = _persist_verdict(db_path, is_match=False)
    db = LandscapeDB.from_url(f"sqlite:///{db_path}", create_tables=False)
    try:
        with db.write_connection() as conn:
            conn.execute(
                update(call_verifications_table)
                .where(call_verifications_table.c.current_call_id == decision.current_call_id)
                .values(differences_json="[]")
            )
    finally:
        db.close()
    result = CliRunner().invoke(app, ["explain", "--run", "current", "--token", token_id, "--database", str(db_path), output_flag])
    assert result.exit_code != 0
    assert isinstance(result.exception, AuditIntegrityError)
    assert "verification differences_json must encode an object" in str(result.exception)
    assert "verification_decisions" not in result.output
