# tests/integration/pipeline/test_unsettled_group_end_of_run_backstop.py
"""elspeth-76e936568e — a bound group whose every member is removed WITHOUT a
loss row must not converge silently.

The end-of-input drain loops while ``has_blocked_barrier_work()`` is true. A
buffered sibling holds a BLOCKED row, so a multi-member group with ONE missing
member fails closed there. But when EVERY member of a group leaves without a
``group_losses`` row nothing is buffered, the proxy reads false, and the run
converged quietly with the group never closed — the single-member group is the
minimal shape, and the ticket asked for exactly this reproduction "at the
executor seam directly", because every graph-level route to it is refused at
build (elspeth-494491978d).

Reproduction: the outside-sink collector pipeline
(``test_rule9_error_to_closer_runtime``), with THE single settlement seam
(``RowProcessor._settle_member_losses``, spec §6.1) patched to a no-op so a
diverted member leaves no loss row. Deterministic: no timing, single worker,
every member is error-disposed by the corpus plugin on its first call.

Measured before the fix (probe, 2026-09-05): both the 1-member and the
3-member case returned a RunResult, ``group_losses`` empty, every EXPAND
group at ``(member_count, 0)`` — status ``failed`` only because every row
errored, indistinguishable from the settled control at the run level.
After the fix the run refuses with ``OrchestrationInvariantError`` naming
closer, group and member, and finalizes FAILED through the same ceremony as
``sweep_deferred_invariants_or_crash`` (its durability test's shape:
``runs.status == FAILED`` after the raise).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.enums import RunStatus
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.engine.processor import RowProcessor
from tests.fixtures.dag_scenario_corpus.plugins import install_corpus_plugin_manager
from tests.integration.pipeline.test_rule9_error_to_closer_runtime import (
    _COLLECTOR_DIVERT_OUT_YAML,
    _COLLECTOR_EXPLICIT_YAML,
    PipelineResult,
    _build_and_run_jsonl,
)

_DOCUMENTS: dict[str, list[dict[str, Any]]] = {
    # The minimal silent shape: one member, nothing left to hold the loop open.
    "single_member": [{"id": 3, "items": [9]}],
    # Every member gone without a loss row: also nothing buffered, also silent.
    "three_members_all_removed": [{"id": 1, "items": [3, 1, 2]}],
}


def _settings_yaml(tmp_path: Path) -> str:
    return _COLLECTOR_DIVERT_OUT_YAML.replace("{rejects_path}", str(tmp_path / "rejects.jsonl"))


def _input_jsonl(documents: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(document) + "\n" for document in documents)


def _run_status_from_db(db_path: Path) -> RunStatus:
    db = LandscapeDB(f"sqlite:///{db_path}")
    try:
        factory = RecorderFactory(db)
        runs = factory.run_lifecycle.list_runs()
        assert len(runs) == 1, runs
        return runs[0].status
    finally:
        db.close()


@pytest.mark.parametrize("shape", sorted(_DOCUMENTS))
def test_unsettled_bound_group_refuses_at_end_of_run_instead_of_converging_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    install_corpus_plugin_manager(monkeypatch)

    def _no_settlement(self: RowProcessor, current_token: Any, reason: str, child_items: list[Any], **_: Any) -> list[Any]:
        return []

    monkeypatch.setattr(RowProcessor, "_settle_member_losses", _no_settlement)

    with pytest.raises(OrchestrationInvariantError) as exc_info:
        _build_and_run_jsonl(_settings_yaml(tmp_path), tmp_path, input_jsonl=_input_jsonl(_DOCUMENTS[shape]))

    message = str(exc_info.value)
    # The verdict is the §8 gate's own text (derived, not restated): closer,
    # group and member named, and the gate's "can never settle" clause.
    assert "bound-group member(s) can never settle" in message
    assert "at closer 'page_stitcher'" in message
    assert "expand group" in message
    assert "elspeth-76e936568e" in message

    # Same failure ceremony as sweep_deferred_invariants_or_crash: the run
    # finalized FAILED, and the durable evidence (the unreconciled group) is
    # still there — nothing was deleted to make the refusal.
    db_path = tmp_path / "audit.db"
    assert _run_status_from_db(db_path) == RunStatus.FAILED
    result = PipelineResult(db_path=db_path, result_data={}, output_rows=[])
    assert result.group_losses() == []
    member_count = len(_DOCUMENTS[shape][0]["items"])
    assert list(result.expand_group_member_and_loss_counts().values()) == [(member_count, 0)]


@pytest.mark.parametrize("shape", sorted(_DOCUMENTS))
def test_settled_control_still_completes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str) -> None:
    """Control: the same pipeline with the seam intact completes — the
    backstop refuses ONLY an unsettled group. (Every row errors here, so
    the run's terminal status is FAILED on both sides of the seam patch;
    what distinguishes the control is that it RETURNS, with every group
    reconciled through the ledger.)"""
    install_corpus_plugin_manager(monkeypatch)

    db_path, result_data, _output_rows = _build_and_run_jsonl(
        _settings_yaml(tmp_path), tmp_path, input_jsonl=_input_jsonl(_DOCUMENTS[shape])
    )
    result = PipelineResult(db_path=db_path, result_data=result_data, output_rows=[])
    member_count = len(_DOCUMENTS[shape][0]["items"])
    assert list(result.expand_group_member_and_loss_counts().values()) == [(member_count, member_count)]


@pytest.mark.parametrize("shape", sorted(_DOCUMENTS))
def test_released_members_pass_the_backstop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str) -> None:
    """Control for the other direction: members that ARRIVE at the collector
    and are released (passthrough in-region transform, every member
    consumed, ``coalesced`` outcomes) must pass the backstop. A released
    member's only journal row is its in-region item, marked BLOCKED in place
    when it reached the barrier — it never gets a row AT the collector node,
    and its address columns are identical to a diverted member's. The gate
    reads the hold stamp (``barrier_blocked_at``), which is why this run
    completes; a position-based reading refused it (measured 2026-09-05)."""
    install_corpus_plugin_manager(monkeypatch)
    settings_yaml = _COLLECTOR_EXPLICIT_YAML.replace("plugin: dag_corpus_branch_loss", "plugin: passthrough").replace(
        "on_error: page_stitcher", "on_error: discard"
    )

    db_path, result_data, output_rows = _build_and_run_jsonl(settings_yaml, tmp_path, input_jsonl=_input_jsonl(_DOCUMENTS[shape]))

    assert result_data["status"] == RunStatus.COMPLETED.value
    member_count = len(_DOCUMENTS[shape][0]["items"])
    result = PipelineResult(db_path=db_path, result_data=result_data, output_rows=output_rows)
    assert result.token_outcome_paths().count(("success", "coalesced")) == member_count
    assert result.group_losses() == []
    assert len(output_rows) == 1  # the collector flushed once for the one group
