"""Topology-specific recovery proof for the partial-terminal fork scenario."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from elspeth.contracts import RunStatus
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import (
    artifacts_table,
    rows_table,
    sink_effect_attempts_table,
    sink_effects_table,
    token_outcomes_table,
    token_parents_table,
    tokens_table,
)
from tests.fixtures.dag_scenario_corpus.harness import (
    SinkBoundaryInterruptedContext,
    run_sink_boundary_recovery_case,
)
from tests.fixtures.dag_scenario_corpus.schema import (
    HarnessCaseSpec,
    ScenarioManifest,
    ScenarioRunEvidence,
    ScenarioSpec,
    SinkBoundaryEffectProjection,
    SinkBoundaryWorkProjection,
    StableRunProjection,
    SummaryRunExpectation,
)

SCENARIO_ID = "fork-multiple-terminals-partial-failure"
CASE_ID = "reopen-after-partial-terminal"
SURVIVOR_ROWS = (
    '{"id":1,"value":10}',
    '{"id":2,"value":20}',
    '{"id":3,"value":30}',
)
REQUIRED_AUDIT_RECORD_TYPES = (
    "artifact",
    "operation",
    "row",
    "run",
    "scheduler_event",
    "sink_effect",
    "sink_effect_member",
    "token",
    "token_outcome",
)


@dataclass(frozen=True, slots=True)
class PartialTerminalTokenLineage:
    """One durable token and its exact fork-parent relationship."""

    token_id: str
    row_id: str
    ingest_sequence: int
    branch_name: str | None
    parent_token_id: str | None
    parent_ordinal: int | None


@dataclass(frozen=True, slots=True)
class PartialTerminalInterruptedEvidence:
    """S5-specific facts captured while the interrupted database is open."""

    checkpoint_sequence: int
    checkpoint_topology_hash: str
    token_lineage: tuple[PartialTerminalTokenLineage, ...]
    work: tuple[SinkBoundaryWorkProjection, ...]
    terminal_outcome_count: int
    failing_effect: SinkBoundaryEffectProjection
    survivor_effect: SinkBoundaryEffectProjection
    survivor_commit_intent_count: int
    survivor_commit_intent_id: str
    survivor_lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class PartialTerminalRecoveryResult:
    """Generic recovery evidence plus the exact S5 topology witnesses."""

    evidence: ScenarioRunEvidence
    interrupted: PartialTerminalInterruptedEvidence
    first_recovery_attempt_started_at: datetime
    interrupted_commit_state_after: str
    resume_window_seconds: float
    final_token_outcome_count: int
    final_work_count: int


def declared_partial_terminal_recovery_case(
    manifest: ScenarioManifest,
) -> tuple[ScenarioSpec, HarnessCaseSpec]:
    """Return the scenario plus its coordinator-ready recovery declaration."""

    scenario = next((candidate for candidate in manifest.scenarios if candidate.id == SCENARIO_ID), None)
    if scenario is None:
        raise AssertionError(f"DAG corpus lacks required scenario {SCENARIO_ID!r}")
    case = HarnessCaseSpec.model_validate(
        {
            "id": CASE_ID,
            "workflow": "recovery",
            "recovery_kind": "sink_boundary",
            "recovery_fault": {
                "kind": "sink_effect",
                "seam": "before_effect",
                "sink_name": "survivor",
                "occurrence": 1,
            },
            "fixture": f"{SCENARIO_ID}/reopen-after-survivor-boundary.yaml",
            "input_fixtures": {
                "primary": f"{SCENARIO_ID}/input.csv",
            },
            "output_artifacts": {
                "failing": {
                    "filename": "failing.jsonl",
                    "presence": "absent",
                },
                "survivor": "survivor.jsonl",
            },
            "expected": {
                "kind": "summary",
                "status": RunStatus.COMPLETED_WITH_FAILURES.value,
                "output_rows": 3,
                "required_audit_record_types": REQUIRED_AUDIT_RECORD_TYPES,
            },
        }
    )
    return scenario, case


def _require_summary_expectation(case: HarnessCaseSpec) -> SummaryRunExpectation:
    expected = case.expected
    if not isinstance(expected, SummaryRunExpectation):
        raise AssertionError("partial-terminal recovery requires a summary expectation")
    return expected


def _capture_interrupted_state(
    context: SinkBoundaryInterruptedContext,
) -> PartialTerminalInterruptedEvidence:
    """Read, but never mutate, exact interrupted S5 topology and effect state."""

    if context.source_names_exhausted != ("primary",):
        raise AssertionError(f"partial-terminal recovery source was not exhausted: {context.source_names_exhausted!r}")
    if context.checkpoint_sequence != 3:
        raise AssertionError(f"partial-terminal recovery requires the sequence-3 checkpoint, got {context.checkpoint_sequence}")
    if context.checkpoint_topology_hash != context.built.graph_evidence.topology_hash:
        raise AssertionError("partial-terminal recovery checkpoint does not bind the interrupted topology")
    if context.rendered.output_paths["failing"].exists() or context.rendered.output_paths["survivor"].exists():
        raise AssertionError("partial-terminal recovery created a physical output before reopen")

    with context.database.connection() as conn:
        token_rows = tuple(
            conn.execute(
                select(
                    tokens_table.c.token_id,
                    tokens_table.c.row_id,
                    tokens_table.c.branch_name,
                    rows_table.c.ingest_sequence,
                )
                .join(
                    rows_table,
                    (rows_table.c.row_id == tokens_table.c.row_id) & (rows_table.c.run_id == tokens_table.c.run_id),
                )
                .where(tokens_table.c.run_id == context.run_id)
                .order_by(rows_table.c.ingest_sequence, tokens_table.c.token_id)
            ).mappings()
        )
        parent_rows = tuple(
            conn.execute(
                select(
                    token_parents_table.c.token_id,
                    token_parents_table.c.parent_token_id,
                    token_parents_table.c.ordinal,
                )
                .where(token_parents_table.c.run_id == context.run_id)
                .order_by(token_parents_table.c.token_id, token_parents_table.c.ordinal)
            ).mappings()
        )
        outcome_rows = tuple(
            conn.execute(
                select(
                    token_outcomes_table.c.token_id,
                    token_outcomes_table.c.completed,
                    token_outcomes_table.c.outcome,
                    token_outcomes_table.c.path,
                    token_outcomes_table.c.sink_name,
                )
                .where(token_outcomes_table.c.run_id == context.run_id)
                .order_by(token_outcomes_table.c.token_id, token_outcomes_table.c.recorded_at)
            ).mappings()
        )
        effect_rows = tuple(
            conn.execute(
                select(
                    sink_effects_table.c.effect_id,
                    sink_effects_table.c.sink_node_id,
                    sink_effects_table.c.state,
                    sink_effects_table.c.lease_expires_at,
                    sink_effects_table.c.publication_performed,
                    sink_effects_table.c.publication_evidence_kind,
                )
                .where(sink_effects_table.c.run_id == context.run_id)
                .order_by(sink_effects_table.c.effect_id)
            ).mappings()
        )
        artifact_rows = tuple(
            conn.execute(
                select(
                    artifacts_table.c.sink_effect_id,
                    artifacts_table.c.publication_performed,
                    artifacts_table.c.publication_evidence_kind,
                    artifacts_table.c.size_bytes,
                )
                .where(artifacts_table.c.run_id == context.run_id)
                .order_by(artifacts_table.c.artifact_id)
            ).mappings()
        )
        effect_ids = tuple(str(row["effect_id"]) for row in effect_rows)
        attempt_rows = tuple(
            conn.execute(
                select(
                    sink_effect_attempts_table.c.effect_id,
                    sink_effect_attempts_table.c.attempt_id,
                    sink_effect_attempts_table.c.action,
                    sink_effect_attempts_table.c.state,
                )
                .where(sink_effect_attempts_table.c.effect_id.in_(effect_ids))
                .order_by(sink_effect_attempts_table.c.effect_id, sink_effect_attempts_table.c.started_at)
            ).mappings()
        )

    if len(token_rows) != 9 or {str(row["token_id"]) for row in token_rows} != set(context.token_ids):
        raise AssertionError("partial-terminal recovery does not contain the exact nine durable tokens")
    parents_by_token: dict[str, tuple[str, int]] = {}
    for row in parent_rows:
        token_id = str(row["token_id"])
        if token_id in parents_by_token:
            raise AssertionError(f"partial-terminal fork child {token_id!r} has multiple parents")
        parents_by_token[token_id] = (str(row["parent_token_id"]), int(row["ordinal"]))
    lineage = tuple(
        PartialTerminalTokenLineage(
            token_id=str(row["token_id"]),
            row_id=str(row["row_id"]),
            ingest_sequence=int(row["ingest_sequence"]),
            branch_name=None if row["branch_name"] is None else str(row["branch_name"]),
            parent_token_id=parents_by_token.get(str(row["token_id"]), (None, None))[0],
            parent_ordinal=parents_by_token.get(str(row["token_id"]), (None, None))[1],
        )
        for row in token_rows
    )
    for ingest_sequence in range(3):
        row_lineage = tuple(item for item in lineage if item.ingest_sequence == ingest_sequence)
        roots = tuple(item for item in row_lineage if item.parent_token_id is None)
        if len(row_lineage) != 3 or len(roots) != 1 or roots[0].branch_name is not None:
            raise AssertionError(f"partial-terminal row {ingest_sequence} lacks one root and two fork children")
        children = tuple(item for item in row_lineage if item.parent_token_id is not None)
        if {(child.branch_name, child.parent_token_id, child.parent_ordinal) for child in children} != {
            ("failing", roots[0].token_id, 0),
            ("survivor", roots[0].token_id, 1),
        }:
            raise AssertionError(f"partial-terminal row {ingest_sequence} has corrupt fork lineage")

    lineage_by_token = {item.token_id: item for item in lineage}
    if len(context.work) != 9 or {item.token_id for item in context.work} != set(context.token_ids):
        raise AssertionError("partial-terminal recovery lacks exact scheduler work for all nine tokens")
    for item in context.work:
        branch = lineage_by_token[item.token_id].branch_name
        expected = {
            None: ("terminal", None, None, None, "purged"),
            # Scheduler material preserves the original sink publication intent.
            # The separate terminal outcome below records the write failure and
            # discard classification.
            "failing": ("terminal", "failing", "success", "default_flow", "purged"),
            "survivor": ("pending_sink", "survivor", "success", "default_flow", "live"),
        }[branch]
        actual = (
            item.status,
            item.pending_sink_name,
            item.pending_outcome,
            item.pending_path,
            item.row_payload_state,
        )
        if actual != expected:
            raise AssertionError(
                f"partial-terminal recovery scheduler work drift for {item.token_id!r}: expected {expected!r}, got {actual!r}"
            )

    terminal_outcomes = tuple(row for row in outcome_rows if row["completed"] == 1)
    if len(terminal_outcomes) != 6:
        raise AssertionError(f"partial-terminal recovery expected six pre-reopen outcomes, got {terminal_outcomes!r}")
    terminal_shapes = {
        (
            lineage_by_token[str(row["token_id"])].branch_name,
            str(row["outcome"]),
            str(row["path"]),
            None if row["sink_name"] is None else str(row["sink_name"]),
        )
        for row in terminal_outcomes
    }
    if terminal_shapes != {
        (None, "transient", "fork_parent", None),
        ("failing", "failure", "sink_discarded", "__discard__"),
    }:
        raise AssertionError(f"partial-terminal recovery has corrupt pre-reopen terminal outcomes: {terminal_shapes!r}")

    effects_by_name = {effect.sink_name: effect for effect in context.effects}
    if set(effects_by_name) != {"failing", "survivor"}:
        raise AssertionError(f"partial-terminal recovery lacks exact sink effects: {context.effects!r}")
    failing_effect = effects_by_name["failing"]
    survivor_effect = effects_by_name["survivor"]
    if failing_effect.state != "finalized" or len(failing_effect.member_token_ids) != 3:
        raise AssertionError("partial-terminal recovery failing effect is not finalized over three members")
    if survivor_effect != context.interrupted_effect or survivor_effect.state != "in_flight":
        raise AssertionError("partial-terminal recovery survivor effect is not the declared in-flight interruption")
    if len(survivor_effect.member_token_ids) != 3:
        raise AssertionError("partial-terminal recovery survivor effect lacks its three exact members")
    failing_lineage = tuple(item for item in lineage if item.branch_name == "failing")
    survivor_lineage = tuple(item for item in lineage if item.branch_name == "survivor")
    if set(failing_effect.member_token_ids) != {item.token_id for item in failing_lineage} or set(failing_effect.member_row_ids) != {
        item.row_id for item in failing_lineage
    }:
        raise AssertionError("partial-terminal recovery failing effect members do not equal the failing branch lineage")
    if set(survivor_effect.member_token_ids) != {item.token_id for item in survivor_lineage} or set(survivor_effect.member_row_ids) != {
        item.row_id for item in survivor_lineage
    }:
        raise AssertionError("partial-terminal recovery survivor effect members do not equal the survivor branch lineage")

    raw_effects = {str(row["sink_node_id"]): row for row in effect_rows}
    raw_failing = raw_effects[failing_effect.sink_node_id]
    raw_survivor = raw_effects[survivor_effect.sink_node_id]
    if (
        raw_failing["state"],
        raw_failing["publication_performed"],
        raw_failing["publication_evidence_kind"],
    ) != ("finalized", False, "virtual"):
        raise AssertionError(f"partial-terminal recovery failing effect is not a finalized virtual outcome: {raw_failing!r}")
    if (
        raw_survivor["state"],
        raw_survivor["publication_performed"],
        raw_survivor["publication_evidence_kind"],
    ) != ("in_flight", None, None):
        raise AssertionError(f"partial-terminal recovery survivor effect is not pre-publication in-flight: {raw_survivor!r}")
    survivor_lease_expires_at = raw_survivor["lease_expires_at"]
    if not isinstance(survivor_lease_expires_at, datetime):
        raise AssertionError("partial-terminal recovery survivor effect lacks a typed lease expiry")
    if survivor_lease_expires_at.tzinfo is None:
        survivor_lease_expires_at = survivor_lease_expires_at.replace(tzinfo=UTC)
    if survivor_lease_expires_at <= datetime.now(UTC):
        raise AssertionError("partial-terminal recovery survivor lease was not live at the interruption witness")

    if len(artifact_rows) != 1 or (
        str(artifact_rows[0]["sink_effect_id"]),
        artifact_rows[0]["publication_performed"],
        artifact_rows[0]["publication_evidence_kind"],
        int(artifact_rows[0]["size_bytes"]),
    ) != (failing_effect.effect_id, False, "virtual", 0):
        raise AssertionError(f"partial-terminal recovery lacks the exact failing virtual artifact: {artifact_rows!r}")
    survivor_commit_intents = tuple(
        row
        for row in attempt_rows
        if str(row["effect_id"]) == survivor_effect.effect_id and row["action"] == "commit" and row["state"] == "intent"
    )
    if len(survivor_commit_intents) != 1:
        raise AssertionError(f"partial-terminal recovery requires one durable survivor commit INTENT: {survivor_commit_intents!r}")
    survivor_commit_intent_id = str(survivor_commit_intents[0]["attempt_id"])

    return PartialTerminalInterruptedEvidence(
        checkpoint_sequence=context.checkpoint_sequence,
        checkpoint_topology_hash=context.checkpoint_topology_hash,
        token_lineage=lineage,
        work=context.work,
        terminal_outcome_count=len(terminal_outcomes),
        failing_effect=failing_effect,
        survivor_effect=survivor_effect,
        survivor_commit_intent_count=len(survivor_commit_intents),
        survivor_commit_intent_id=survivor_commit_intent_id,
        survivor_lease_expires_at=survivor_lease_expires_at,
    )


def _require_final_projection(evidence: ScenarioRunEvidence) -> StableRunProjection:
    projection = evidence.runtime.durable_projection
    if projection is None:
        raise AssertionError("partial-terminal recovery lacks its durable final projection")
    if evidence.audit.portable_projection != projection:
        raise AssertionError("partial-terminal recovery durable and export projections differ")
    return projection


def _read_recovery_attempt_timing(
    tmp_path: Path,
    interrupted: PartialTerminalInterruptedEvidence,
) -> tuple[str, datetime]:
    """Prove recovery did not supersede the interrupted live lease early."""

    database = LandscapeDB.from_url(
        f"sqlite:///{tmp_path / 'audit.db'}",
        create_tables=False,
        read_only=True,
    )
    try:
        with database.connection() as conn:
            rows = tuple(
                conn.execute(
                    select(
                        sink_effect_attempts_table.c.attempt_id,
                        sink_effect_attempts_table.c.action,
                        sink_effect_attempts_table.c.state,
                        sink_effect_attempts_table.c.started_at,
                    )
                    .where(sink_effect_attempts_table.c.effect_id == interrupted.survivor_effect.effect_id)
                    .order_by(
                        sink_effect_attempts_table.c.started_at,
                        sink_effect_attempts_table.c.attempt_id,
                    )
                ).mappings()
            )
    finally:
        database.close()

    interrupted_attempt = tuple(row for row in rows if str(row["attempt_id"]) == interrupted.survivor_commit_intent_id)
    if len(interrupted_attempt) != 1:
        raise AssertionError("partial-terminal recovery lost the interrupted commit attempt identity")
    interrupted_state_after = str(interrupted_attempt[0]["state"])
    if interrupted_state_after != "response_lost":
        raise AssertionError("partial-terminal recovery did not close the abandoned commit INTENT as response_lost")
    interrupted_started_at = interrupted_attempt[0]["started_at"]
    if not isinstance(interrupted_started_at, datetime):
        raise AssertionError("partial-terminal interrupted commit attempt lacks a typed start time")
    if interrupted_started_at.tzinfo is None:
        interrupted_started_at = interrupted_started_at.replace(tzinfo=UTC)

    recovery_attempts: list[datetime] = []
    for row in rows:
        started_at = row["started_at"]
        if not isinstance(started_at, datetime):
            raise AssertionError("partial-terminal recovery attempt lacks a typed start time")
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        if (
            str(row["attempt_id"]) != interrupted.survivor_commit_intent_id
            and row["action"] in {"reconcile", "commit"}
            and started_at > interrupted_started_at
        ):
            recovery_attempts.append(started_at)
    if not recovery_attempts:
        raise AssertionError("partial-terminal recovery recorded no post-interruption reconcile or commit attempt")
    first_recovery_attempt_started_at = min(recovery_attempts)
    if first_recovery_attempt_started_at < interrupted.survivor_lease_expires_at:
        raise AssertionError("partial-terminal recovery superseded the interrupted effect before its live lease expired")
    return interrupted_state_after, first_recovery_attempt_started_at


def run_partial_terminal_recovery_case(
    scenario: ScenarioSpec,
    case: HarnessCaseSpec,
    tmp_path: Path,
) -> PartialTerminalRecoveryResult:
    """Run the shared public reopen path and enforce S5-specific exactness."""

    if (scenario.id, case.id) != (SCENARIO_ID, CASE_ID):
        raise AssertionError(f"partial-terminal recovery received the wrong declaration: {(scenario.id, case.id)!r}")
    expected = _require_summary_expectation(case)
    interrupted: list[PartialTerminalInterruptedEvidence] = []
    resume_window_started: list[float] = []

    def capture(context: SinkBoundaryInterruptedContext) -> None:
        interrupted.append(_capture_interrupted_state(context))
        resume_window_started.append(time.monotonic())

    evidence = run_sink_boundary_recovery_case(
        scenario,
        case,
        tmp_path,
        before_reopen_verifier=capture,
    )
    if len(interrupted) != 1:
        raise AssertionError(f"partial-terminal recovery captured {len(interrupted)} interrupted states")
    if len(resume_window_started) != 1:
        raise AssertionError("partial-terminal recovery lacks its bounded resume-window witness")
    resume_window_seconds = time.monotonic() - resume_window_started[0]
    if resume_window_seconds >= 15:
        raise AssertionError(f"partial-terminal recovery exceeded its bounded resume window: {resume_window_seconds:.3f}s")
    interrupted_commit_state_after, first_recovery_attempt_started_at = _read_recovery_attempt_timing(tmp_path, interrupted[0])
    if (
        evidence.runtime.status,
        evidence.runtime.rows_processed,
        evidence.runtime.rows_succeeded,
        evidence.runtime.rows_failed,
        evidence.runtime.output_rows,
    ) != (RunStatus.COMPLETED_WITH_FAILURES.value, 3, 3, 3, expected.output_rows):
        raise AssertionError(f"partial-terminal recovery returned wrong final counters: {evidence.runtime!r}")
    if tuple((output.sink_name, output.rows) for output in evidence.runtime.sink_outputs) != (("survivor", SURVIVOR_ROWS),):
        raise AssertionError(f"partial-terminal recovery published the wrong survivor rows: {evidence.runtime.sink_outputs!r}")
    if tmp_path.joinpath("failing.jsonl").exists():
        raise AssertionError("partial-terminal recovery created a physical failing-sink output")
    survivor_lines = tuple(tmp_path.joinpath("survivor.jsonl").read_text(encoding="utf-8").splitlines())
    if tuple(json.loads(line) for line in survivor_lines) != (
        {"id": 1, "value": 10},
        {"id": 2, "value": 20},
        {"id": 3, "value": 30},
    ):
        raise AssertionError(f"partial-terminal recovery did not publish the survivor rows exactly once: {survivor_lines!r}")

    proof = evidence.recovery.sink_boundary
    if proof is None:
        raise AssertionError("partial-terminal recovery lacks generic sink-boundary evidence")
    if (
        proof.fault.sink_name,
        proof.source_names_exhausted_before,
        len(proof.token_ids_before),
        len(proof.work_before),
        {item.status for item in proof.work_before},
        len(proof.effects_before),
        proof.artifact_count_before,
        proof.publication_count_before,
        len(proof.work_after),
        {item.status for item in proof.work_after},
        len(proof.effects_after),
        proof.artifact_count_after,
        proof.publication_count_after,
    ) != (
        "survivor",
        ("primary",),
        9,
        9,
        {"terminal", "pending_sink"},
        2,
        1,
        0,
        9,
        {"terminal"},
        2,
        2,
        1,
    ):
        raise AssertionError(f"partial-terminal generic recovery proof drifted: {proof!r}")
    if proof.token_ids_after != proof.token_ids_before:
        raise AssertionError("partial-terminal recovery reminted token identity")
    if any(item.row_payload_state != "purged" for item in proof.work_after):
        raise AssertionError("partial-terminal recovery did not purge all terminal scheduler payloads")
    if (
        proof.checkpoint_topology_hash != interrupted[0].checkpoint_topology_hash
        or not proof.durable_identity_reused
        or not proof.durable_export_parity
        or not evidence.recovery.checkpoint_removed
        or evidence.audit.source_operation_count != 1
    ):
        raise AssertionError("partial-terminal recovery lost its topology, identity, checkpoint, source, or export proof")

    projection = _require_final_projection(evidence)
    if len(projection.tokens) != 9 or len(projection.terminal_dispositions) != 9:
        raise AssertionError("partial-terminal recovery final projection lacks nine token outcomes")
    if len(projection.scheduler_work) != 9 or any(work.final_status != "terminal" for work in projection.scheduler_work):
        raise AssertionError("partial-terminal recovery final projection lacks nine terminal work items")
    disposition_shapes = tuple(
        (disposition.token_key, disposition.outcome, disposition.path, disposition.sink_name)
        for disposition in projection.terminal_dispositions
    )
    if disposition_shapes != (
        ("primary:0#0", "transient", "fork_parent", None),
        ("primary:0#1", "failure", "sink_discarded", "__discard__"),
        ("primary:0#2", "success", "default_flow", "survivor"),
        ("primary:1#0", "transient", "fork_parent", None),
        ("primary:1#1", "failure", "sink_discarded", "__discard__"),
        ("primary:1#2", "success", "default_flow", "survivor"),
        ("primary:2#0", "transient", "fork_parent", None),
        ("primary:2#1", "failure", "sink_discarded", "__discard__"),
        ("primary:2#2", "success", "default_flow", "survivor"),
    ):
        raise AssertionError(f"partial-terminal recovery changed terminal meaning: {disposition_shapes!r}")

    record_types = {record.record_type for record in evidence.audit.record_counts}
    if not set(expected.required_audit_record_types) <= record_types:
        raise AssertionError(
            f"partial-terminal recovery omitted required audit record types: {set(expected.required_audit_record_types) - record_types!r}"
        )
    stable_material = {
        record.key: json.loads(record.material) for record in projection.audit_records if record.record_type in {"artifact", "sink_effect"}
    }
    failing_effect_material = next(material for key, material in stable_material.items() if key.startswith("sink_effect|sink:failing@"))
    survivor_effect_material = next(material for key, material in stable_material.items() if key.startswith("sink_effect|sink:survivor@"))
    failing_artifact_material = next(material for key, material in stable_material.items() if key.startswith("artifact|sink:failing@"))
    if (
        failing_effect_material["state"],
        failing_effect_material["publication_performed"],
        failing_effect_material["publication_evidence_kind"],
        failing_artifact_material["publication_performed"],
        failing_artifact_material["publication_evidence_kind"],
    ) != ("finalized", False, "virtual", False, "virtual"):
        raise AssertionError("partial-terminal recovery did not preserve the failed terminal's virtual evidence")
    if (
        survivor_effect_material["state"],
        survivor_effect_material["publication_performed"],
        survivor_effect_material["publication_evidence_kind"],
    ) != ("finalized", True, "returned"):
        raise AssertionError("partial-terminal recovery survivor publication is not one finalized returned effect")

    return PartialTerminalRecoveryResult(
        evidence=evidence,
        interrupted=interrupted[0],
        first_recovery_attempt_started_at=first_recovery_attempt_started_at,
        interrupted_commit_state_after=interrupted_commit_state_after,
        resume_window_seconds=resume_window_seconds,
        final_token_outcome_count=len(projection.terminal_dispositions),
        final_work_count=len(projection.scheduler_work),
    )
