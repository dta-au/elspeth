"""A failed coalesce/row_union group is counted per consumed token, live == audit.

Every arm that fails a barrier group — arrival intake, live loss notification,
durable loss replay, timeout/EOF sweep, and a straggler into a group that failed
with no arrived member — must surface one (FAILURE, UNROUTED) result per consumed
token, so the live loop counters equal the audit derive. Before the fix the
intake arm surfaced only the ARRIVING token: any arrival-completed group failure
with two or more arrived members ended an otherwise healthy run with exit 4
("Live-vs-audit terminal counter mismatch"), and the arrival-time failures were
never counted in the live ``rows_coalesce_failed`` at all.

Each case runs the real CLI (``elspeth run --execute``) and records the live and
audit counters handed to ``assert_terminal_counter_parity``: the strict fields
must agree (else the run exits 4) and ``rows_coalesce_failed`` — tolerated and
only logged on mismatch — must agree too.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts.run_result import RunResult
from elspeth.engine.orchestrator import run_lifecycle
from elspeth.engine.orchestrator.types import ExecutionCounters

_CSV_ROWS = "id,price\n1,10\n2,20\n3,30\n"


@dataclass(frozen=True)
class _ParityCall:
    live_rows_failed: int
    audit_rows_failed: int
    live_coalesce_failed: int
    audit_coalesce_failed: int


@dataclass(frozen=True)
class _Run:
    exit_code: int
    output: str
    parity: tuple[_ParityCall, ...]
    db_path: Path


def _fork_gate(branches: list[str]) -> dict[str, Any]:
    return {"name": "fork_gate", "input": "raw", "condition": "True", "routes": {"true": "fork", "false": "out"}, "fork_to": branches}


def _passthrough(name: str, branch: str, out: str) -> dict[str, Any]:
    return {
        "name": name,
        "plugin": "passthrough",
        "input": branch,
        "on_success": out,
        "on_error": "discard",
        "options": {"schema": {"mode": "observed"}},
    }


def _value_transform(name: str, branch: str, out: str, *, target: str, expression: str) -> dict[str, Any]:
    return {
        "name": name,
        "plugin": "value_transform",
        "input": branch,
        "on_success": out,
        "on_error": "discard",
        "options": {"schema": {"mode": "observed"}, "operations": [{"target": target, "expression": expression}]},
    }


# path_a discards row 2 (price 20 -> division by zero -> on_error: discard).
_LOSE_ROW_2 = "100 // (row['price'] - 20)"


def _settings(tmp_path: Path, *, source: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    return {
        "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
        "sources": {"src": source},
        **body,
        "sinks": {
            "out": {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {"path": str(tmp_path / "out.jsonl"), "format": "jsonl", "schema": {"mode": "observed"}},
            }
        },
    }


def _csv_source(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "in.csv"
    path.write_text(_CSV_ROWS)
    return {
        "plugin": "csv",
        "on_success": "raw",
        "options": {
            "path": str(path),
            "on_validation_failure": "discard",
            "schema": {"mode": "fixed", "fields": ["id: int", "price: int"]},
        },
    }


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: dict[str, Any]) -> _Run:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "coalesce-group-failure-accounting")
    calls: list[_ParityCall] = []
    original = run_lifecycle.assert_terminal_counter_parity

    def _recording_parity(*, live: RunResult, audit: ExecutionCounters, run_id: str) -> None:
        calls.append(
            _ParityCall(
                live_rows_failed=live.rows_failed,
                audit_rows_failed=audit.rows_failed,
                live_coalesce_failed=live.rows_coalesce_failed,
                audit_coalesce_failed=audit.rows_coalesce_failed,
            )
        )
        original(live=live, audit=audit, run_id=run_id)

    monkeypatch.setattr(run_lifecycle, "assert_terminal_counter_parity", _recording_parity)
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(yaml.safe_dump(settings, sort_keys=False))
    result = CliRunner().invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute"])
    return _Run(exit_code=result.exit_code, output=result.output, parity=tuple(calls), db_path=tmp_path / "audit.db")


def _non_terminal_token_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        (count,) = conn.execute(
            "SELECT count(*) FROM tokens t "
            "WHERE NOT EXISTS (SELECT 1 FROM token_outcomes o WHERE o.token_id = t.token_id AND o.completed = 1)"
        ).fetchone()
    return int(count)


def _failure_reasons_at_barrier(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT ns.error_json FROM node_states ns JOIN nodes n ON n.node_id = ns.node_id AND n.run_id = ns.run_id "
            "WHERE n.node_type IN ('coalesce', 'row_union') AND ns.status = 'failed'"
        ).fetchall()
    return {json.loads(error_json)["failure_reason"] for (error_json,) in rows}


def _assert_counted_once_per_token(run: _Run, *, exit_code: int, rows_failed: int, coalesce_failed: int) -> None:
    assert run.exit_code == exit_code, run.output
    assert run.parity == (
        _ParityCall(
            live_rows_failed=rows_failed,
            audit_rows_failed=rows_failed,
            live_coalesce_failed=coalesce_failed,
            audit_coalesce_failed=coalesce_failed,
        ),
    )
    assert _non_terminal_token_count(run.db_path) == 0


def test_arrival_completed_failure_with_two_arrived_members_counts_every_member(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """quorum 2 of 3, merge select path_a: on row 2 path_a is lost and the
    group fails ``select_branch_not_arrived`` when the second of path_b/path_c
    arrives — two arrived members, completed by an arrival (the intake arm).
    Before the fix: exit 4, rows_failed live 4 / audit 5."""
    body = {
        "gates": [_fork_gate(["path_a", "path_b", "path_c"])],
        "transforms": [
            _value_transform("vt_a", "path_a", "out_a", target="bonus", expression=_LOSE_ROW_2),
            _passthrough("pt_b", "path_b", "out_b"),
            _passthrough("pt_c", "path_c", "out_c"),
        ],
        "coalesce": [
            {
                "name": "merge_results",
                "branches": {"path_a": "out_a", "path_b": "out_b", "path_c": "out_c"},
                "policy": "quorum",
                "quorum_count": 2,
                "merge": "select",
                "select_branch": "path_a",
                "on_success": "out",
            }
        ],
    }
    run = _run(tmp_path, monkeypatch, _settings(tmp_path, source=_csv_source(tmp_path), body=body))

    # rows_failed: row 2's quarantined path_a token + its 2 failed members,
    # plus the late third arrival on each of rows 1 and 3 (merged at quorum) = 5.
    # rows_coalesce_failed: row 2's group only (a late arrival after a merge
    # is not a failed barrier).
    _assert_counted_once_per_token(run, exit_code=1, rows_failed=5, coalesce_failed=1)
    assert "select_branch_not_arrived" in _failure_reasons_at_barrier(run.db_path)


def test_observed_rewrite_type_conflict_routes_every_row_never_exit_4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An observed source's value_transform rewrite (declared ``any``) meets a
    passthrough's inferred ``int`` at a union coalesce: the conflict is only
    knowable at row 1, so every group fails ``contract_type_conflict`` at
    arrival and every row fails. The run ends FAILED (exit 2: nothing
    succeeded) — before the fix it ended exit 4 on the counter mismatch."""
    source_path = tmp_path / "in.jsonl"
    source_path.write_text('{"id": 1, "q": 10}\n{"id": 2, "q": 20}\n')
    source = {
        "plugin": "json",
        "on_success": "raw",
        "options": {"path": str(source_path), "format": "jsonl", "on_validation_failure": "discard", "schema": {"mode": "observed"}},
    }
    body = {
        "gates": [_fork_gate(["path_a", "path_b"])],
        "transforms": [
            _value_transform("vt_a", "path_a", "out_a", target="q", expression="row['q'] + 1"),
            _passthrough("pt_b", "path_b", "out_b"),
        ],
        "coalesce": [
            {
                "name": "merge_results",
                "branches": {"path_a": "out_a", "path_b": "out_b"},
                "policy": "require_all",
                "merge": "union",
                "on_success": "out",
            }
        ],
    }
    run = _run(tmp_path, monkeypatch, _settings(tmp_path, source=source, body=body))

    _assert_counted_once_per_token(run, exit_code=2, rows_failed=4, coalesce_failed=2)
    reasons = _failure_reasons_at_barrier(run.db_path)
    assert len(reasons) == 1
    assert next(iter(reasons)).startswith("contract_type_conflict: ")


@pytest.mark.parametrize("branches", [["path_a", "path_b"], ["path_a", "path_b", "path_c"]], ids=["two_branches", "three_branches"])
def test_group_lost_before_any_arrival_is_counted_at_its_first_straggler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, branches: list[str]
) -> None:
    """require_all: path_a is lost on row 2 BEFORE any sibling arrives, so the
    group fails with zero arrived members and writes nothing at the barrier;
    each sibling then straggles in as a late arrival. The audit derive counts
    the failed barrier once, from the first straggler's FAILED state — so
    does the live counter (never once per straggler, never zero)."""
    siblings = [branch for branch in branches if branch != "path_a"]
    body = {
        "gates": [_fork_gate(branches)],
        "transforms": [
            _value_transform("vt_a", "path_a", "out_a", target="bonus", expression=_LOSE_ROW_2),
            *[_passthrough(f"pt_{branch}", branch, f"out_{branch}") for branch in siblings],
        ],
        "coalesce": [
            {
                "name": "merge_results",
                "branches": {"path_a": "out_a", **{branch: f"out_{branch}" for branch in siblings}},
                "policy": "require_all",
                "merge": "union",
                "on_success": "out",
            }
        ],
    }
    run = _run(tmp_path, monkeypatch, _settings(tmp_path, source=_csv_source(tmp_path), body=body))

    # the quarantined path_a token + one late FAILURE per sibling on row 2
    _assert_counted_once_per_token(run, exit_code=1, rows_failed=1 + len(siblings), coalesce_failed=1)
    assert _failure_reasons_at_barrier(run.db_path) == {"scope_group_failed"}


def test_row_union_group_lost_before_any_arrival_is_counted_at_its_first_straggler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The row_union twin: a branch lost before any sibling arrives marks the
    group dead with nothing held; the sibling's late FAILED state is the
    group's first barrier evidence and carries its one live count."""
    body = {
        "gates": [_fork_gate(["path_a", "path_b"])],
        "transforms": [
            _value_transform("vt_a", "path_a", "out_a", target="bonus", expression=_LOSE_ROW_2),
            _passthrough("pt_b", "path_b", "out_b"),
            _passthrough("pt_after", "unioned", "out"),
        ],
        "row_unions": [{"name": "union_results", "branches": {"path_a": "out_a", "path_b": "out_b"}, "on_success": "unioned"}],
    }
    run = _run(tmp_path, monkeypatch, _settings(tmp_path, source=_csv_source(tmp_path), body=body))

    _assert_counted_once_per_token(run, exit_code=1, rows_failed=2, coalesce_failed=1)
    assert _failure_reasons_at_barrier(run.db_path) == {"row_union_branch_lost"}
