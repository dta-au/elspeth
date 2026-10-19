"""CSV retention must fail before publication when a required row is refused."""

from __future__ import annotations

import csv
import traceback
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.sink_effects import (
    RestrictedSinkEffectContext,
    SinkEffectInspectionRequest,
    SinkEffectMember,
    SinkEffectPipelineMembersInput,
    SinkEffectPlan,
    SinkEffectPrepareRequest,
)
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.sinks.csv_sink import CSVSink, CSVSinkConfig
from tests.fixtures.base_classes import inject_write_failure


def _prepare(sink: CSVSink, rows: list[dict[str, object]]) -> tuple[SinkEffectPlan, RestrictedSinkEffectContext]:
    effect_id = "ab" * 32
    ctx = RestrictedSinkEffectContext(
        run_id="run-required-rows",
        run_started_at=datetime(2026, 10, 18, tzinfo=UTC),
        operation_id="operation-required-rows",
        sink_node_id="sink-required-rows",
    )
    members = tuple(
        SinkEffectMember(
            ordinal=index,
            token_id=f"token-{index}",
            row_id=f"row-{index}",
            ingest_sequence=index,
            lineage_json="[]",
            lineage_hash=sha256(b"[]").hexdigest(),
            payload_hash=sha256(canonical_json(row).encode()).hexdigest(),
            row=row,
            member_effect_id=sha256(f"member-{index}".encode()).hexdigest(),
        )
        for index, row in enumerate(rows)
    )
    inspection = sink.inspect_effect(SinkEffectInspectionRequest(effect_id=effect_id, target="{}", predecessor_descriptor=None), ctx)
    plan = sink.prepare_effect(
        SinkEffectPrepareRequest(
            effect_id=effect_id,
            inspection=inspection,
            effect_input=SinkEffectPipelineMembersInput(
                members=members, target_snapshot_members=members, target_delivered_member_count=len(members)
            ),
        ),
        ctx,
    )
    return plan, ctx


@pytest.mark.parametrize("mode", ["write", "append"])
@pytest.mark.parametrize("refusal", ["shape", "encoding", "serialization"])
def test_required_rows_refusal_keeps_preimage_and_removes_staging(
    tmp_path: Path, mode: str, refusal: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "retained.csv"
    original = b"value\npreserved\n"
    target.write_bytes(original)
    inode = target.stat().st_ino
    sink = inject_write_failure(
        CSVSink(
            {
                "path": str(target),
                "schema": {"mode": "observed"},
                "mode": mode,
                "encoding": "ascii" if refusal == "encoding" else "utf-8",
                "require_all_rows": True,
            }
        )
    )
    rows: list[dict[str, object]] = [{"value": "accepted"}, {"value": "refused"}]
    if refusal == "shape":
        rows[1]["extra"] = "private test value"
    elif refusal == "encoding":
        rows[1]["value"] = "private test value \u2603"
    else:

        def refuse_serialization(_self: csv.DictWriter, _row: dict[str, object]) -> None:
            raise csv.Error("private test value")

        monkeypatch.setattr(csv.DictWriter, "writerow", refuse_serialization)

    with pytest.raises(RuntimeError, match="require_all_rows") as exc_info:
        _prepare(sink, rows)

    assert "private test value" not in str(exc_info.value)
    rendered = "".join(traceback.format_exception(exc_info.value))
    assert "private test value" not in rendered
    assert "During handling of the above exception" not in rendered
    assert "codec can't encode" not in rendered
    assert target.read_bytes() == original
    assert target.stat().st_ino == inode
    assert list(tmp_path.iterdir()) == [target]
    assert sink._get_diversions() == ()


@pytest.mark.parametrize("require_all_rows", [None, False])
def test_default_and_explicit_false_keep_existing_row_diversion(tmp_path: Path, require_all_rows: bool | None) -> None:
    options: dict[str, object] = {"path": str(tmp_path / "retained.csv"), "schema": {"mode": "observed"}}
    if require_all_rows is not None:
        options["require_all_rows"] = require_all_rows
    sink = inject_write_failure(CSVSink(options))
    plan, ctx = _prepare(sink, [{"value": "accepted"}, {"value": "refused", "extra": "preserved diversion"}])
    assert plan.safe_evidence["accepted_ordinals"] == (0,)
    assert plan.safe_evidence["diverted_ordinals"] == (1,)
    sink.commit_effect(plan, ctx)
    assert (tmp_path / "retained.csv").read_text() == "value\naccepted\n"


def test_required_rows_accepts_and_commits_valid_rows(tmp_path: Path) -> None:
    target = tmp_path / "retained.csv"
    sink = inject_write_failure(CSVSink({"path": str(target), "schema": {"mode": "observed"}, "require_all_rows": True}))
    plan, ctx = _prepare(sink, [{"value": "one"}, {"value": "two"}])
    assert plan.safe_evidence["accepted_ordinals"] == (0, 1)
    assert plan.safe_evidence["diverted_ordinals"] == ()
    sink.commit_effect(plan, ctx)
    assert target.read_text() == "value\none\ntwo\n"


@pytest.mark.parametrize("invalid", ["true", "false", 1, 0, None])
def test_required_rows_option_refuses_non_boolean_values(invalid: object) -> None:
    with pytest.raises(PluginConfigError, match="require_all_rows"):
        CSVSinkConfig.from_dict({"path": "retained.csv", "schema": {"mode": "observed"}, "require_all_rows": invalid})
