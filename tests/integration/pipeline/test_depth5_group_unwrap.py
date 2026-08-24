# tests/integration/pipeline/test_depth5_group_unwrap.py
"""Spec §6.3 acceptance scenario at the SUPPORTED depth guarantee: five
nested all-require_all bound regions; a single token failing in the fifth
layer unwraps level by level — each verdict escalating one frame outward —
until the outermost group's declared terminal handling fires. Correctness is
depth-independent by design; 5 is the builder-enforced supported ceiling
(``ElspethSettings.max_bound_region_depth`` default, spec §6.3).

Topology (``_nested_settings(depth)``): a chain of ``fork_k`` gates,
``k = 1..depth``. Each fork splits into two branches, ``go_k`` (recursing to
``fork_{k+1}``, or into the always-failing ``poison`` transform at the
innermost level) and ``ok_k`` (a passthrough survivor that feeds straight
into that level's own closer, ``merge_k``). Each ``merge_k`` closes its own
``fork_k``'s roster (``require_all``) and, for every level but the
outermost, is referenced BY NAME as the ``go_{k-1}`` branch source of the
enclosing ``merge_{k-1}`` — the same "nested coalesce feeds an outer
coalesce's branch, by node name, no ``on_success`` needed" wiring pinned by
``test_nested_group_settlement.py``'s one-level fixture, generalized to
depth. Only the OUTERMOST ``merge_1`` carries ``on_success`` (routing to the
sink) — every inner ``merge_k`` is consumed purely by name.

Cardinality note: the absolute counts pinned below (one ``quarantined`` loss,
``depth - 1`` ``group_failed`` escalations) hold ONLY at a single source row
— see ``test_nested_group_settlement.py``'s ``_Topology`` docstring for the
same point made about its one-level fixture. Do not "fix" this module by
widening the source to more rows; that changes what the pins mean, it does
not generalize them.

``_nested_settings`` / ``build_settings_document`` are IMPORTED by the
WS5/WS6 crash+resume variant (``docs/superpowers/plans/
2026-08-21-unified-lineage-ws5-ws6-resume-observability.md``) — both stay
plain module-level functions, fixture-free, returning the settings DOCUMENT
(dict) shape ``load_settings_from_config_dict`` consumes (mirrors
``examples/fork_coalesce/settings.yaml``'s vocabulary: ``fork_to``,
``branches``, ``policy``, ``on_error``). Real corpus plugin names
(``dag_corpus_always_error``) come from
``tests/fixtures/dag_scenario_corpus/plugins.py``; ``passthrough`` is a real
builtin, not a corpus plugin.

``flush_iterations_used`` probe choice: ``RowProcessor.run_barrier_intake``
(``elspeth/engine/processor.py``) has exactly ONE caller anywhere in the
tree — ``run_end_of_input_barrier_flush``'s convergence loop
(``elspeth/engine/orchestrator/leader_drain.py``) — the per-item drain path
(``scheduler_drain.py``) calls the private ``_run_barrier_intake_pass``
directly, never the public wrapper. So counting calls to the public method,
via a ``monkeypatch.setattr`` wrapper installed on the ``RowProcessor``
CLASS before the run, is an exact 1:1 proxy for EOF-flush loop iterations
with no new production hook and no dependency on which module imported the
flush function by name (a class-attribute patch is resolved at call time,
not import time — unlike patching the free function, which the finalize
suite's ``time.monotonic``/``time.sleep`` precedent does not need to worry
about since those ARE free functions read fresh each call).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select

from elspeth.contracts.sink_effects import SinkEffectExecutionPurpose, SinkEffectInputKind
from elspeth.core.config import load_settings_from_config_dict
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.bound_regions import derive_escalation_fixpoint_bound
from elspeth.core.dag.models import GraphValidationError
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import group_losses_table, token_outcomes_table, tokens_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import (
    assemble_and_validate_pipeline_config,
    execution_sink_bindings_for_runtime,
    execution_sinks_for_runtime,
    sink_effect_modes_from_runtime_bindings,
    validate_pipeline_sink_effect_capabilities,
)
from elspeth.engine.processor import RowProcessor
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config
from elspeth.plugins.transforms.llm.model_catalog import read_openrouter_catalog_snapshot_id
from tests.fixtures.dag_scenario_corpus.plugins import install_corpus_plugin_manager

DEPTH = 5
_SOURCE_CSV = "id,value\n1,10\n"  # single row (see module docstring cardinality note)


def build_settings_document(
    *, gates: list[dict[str, Any]], transforms: list[dict[str, Any]], coalesces: list[dict[str, Any]], sink: str
) -> dict[str, Any]:
    """~30-line settings-document assembler (spec §6.3 Task 10 brief): wraps
    gates/transforms/coalesce in the full settings mapping
    ``load_settings_from_config_dict`` consumes, with a single CSV source and
    a single JSON sink, key names mirroring ``examples/fork_coalesce/
    settings.yaml``. ``{input_path}``/``{output_path}`` are placeholders the
    ``run_settings``/``build_graph`` fixtures substitute with real per-case
    paths before loading — plain-function callers (the WS5/WS6 import) get
    the unsubstituted document back, exactly as this function's contract
    promises (no fixture arguments, no test-local state)."""
    return {
        "sources": {
            "primary": {
                "plugin": "csv",
                "on_success": "fork_input",
                "options": {
                    "path": "{input_path}",
                    "on_validation_failure": "discard",
                    "schema": {"mode": "fixed", "fields": ["id: int", "value: int"]},
                },
            }
        },
        "concurrency": {"max_workers": 1},
        "gates": gates,
        "transforms": transforms,
        "coalesce": coalesces,
        "sinks": {
            sink: {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {
                    "path": "{output_path}",
                    "format": "jsonl",
                    "schema": {"mode": "observed"},
                },
            }
        },
    }


def _nested_settings(depth: int) -> dict[str, Any]:
    """fork_1 -> ... -> fork_depth -> quarantining transform on the innermost
    'go' branch -> merge_depth -> ... -> merge_1 -> sink. Each fork's whole
    roster closes at its own coalesce (ruling 23), regions well-nested
    (§7 rule 3), every policy require_all. Two branches per level ('go_k'
    recursing deeper, 'ok_k' a passthrough) keeps the tree small: the
    failure sits on the innermost 'go' line; every 'ok' sibling is a
    survivor that closes at its OWN level's merge, never propagating past
    it directly (only via that merge's own group-failure escalation)."""
    gates: list[dict[str, Any]] = []
    transforms: list[dict[str, Any]] = []
    coalesces: list[dict[str, Any]] = []
    for k in range(1, depth + 1):
        upstream_input = "fork_input" if k == 1 else f"go_{k - 1}"
        gates.append(
            {
                "name": f"fork_{k}",
                "input": upstream_input,
                "condition": "True",
                "routes": {"true": "fork", "false": "discard"},
                "fork_to": [f"go_{k}", f"ok_{k}"],
            }
        )
        transforms.append(
            {
                "name": f"tag_ok_{k}",
                "plugin": "passthrough",
                "input": f"ok_{k}",
                "on_success": f"tag_ok_{k}_out",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            }
        )
        go_source = "poison_out" if k == depth else f"merge_{k + 1}"
        coalesce_entry: dict[str, Any] = {
            "name": f"merge_{k}",
            "branches": {f"go_{k}": go_source, f"ok_{k}": f"tag_ok_{k}_out"},
            "policy": "require_all",
            # 'nested' (not 'union'): union enforces cross-branch schema
            # compatibility, and the poison branch's explicit CorpusInputSchema
            # collides with the passthrough branch's observed schema — a
            # build-time rejection unrelated to settlement semantics (matches
            # test_nested_group_settlement.py's fixture 2, same reason).
            "merge": "nested",
        }
        if k == 1:
            # Only the outermost closer routes to the sink by name; every
            # inner merge_k is consumed purely by node-name reference from
            # its enclosing merge_{k-1} (the precedent's no-on_success
            # nested-coalesce wiring, generalized to depth).
            coalesce_entry["on_success"] = "results"
        coalesces.append(coalesce_entry)
    transforms.append(
        # dag_corpus_always_error: quarantines the innermost go-line token
        # via its on_error: discard — the loss producer for the single
        # 'quarantined' ledger row.
        {
            "name": "poison",
            "plugin": "dag_corpus_always_error",
            "input": f"go_{depth}",
            "on_success": "poison_out",
            "on_error": "discard",
            "options": {"schema": {"mode": "observed"}},
        }
    )
    return build_settings_document(gates=gates, transforms=transforms, coalesces=coalesces, sink="results")


def _substitute_paths(node: Any, *, input_path: Path, output_path: Path) -> Any:
    if isinstance(node, dict):
        return {key: _substitute_paths(value, input_path=input_path, output_path=output_path) for key, value in node.items()}
    if isinstance(node, list):
        return [_substitute_paths(value, input_path=input_path, output_path=output_path) for value in node]
    if node == "{input_path}":
        return str(input_path)
    if node == "{output_path}":
        return str(output_path)
    return node


def _build_graph_from_document(settings_document: dict[str, Any], case_dir: Path) -> tuple[ExecutionGraph, Any, Any]:
    """Build (not run) the real production graph from a settings DOCUMENT
    (dict), via ``load_settings_from_config_dict`` — "the normal settings
    loader" the settings-document builder above targets. Mirrors the build
    half of ``test_nested_group_settlement.py``'s ``_build_and_run``, minus
    the YAML-string ``.format()`` step (a dict has no ``.format``; path
    substitution is ``_substitute_paths`` instead)."""
    input_path = case_dir / "input.csv"
    output_path = case_dir / "output.jsonl"
    input_path.write_text(_SOURCE_CSV)
    resolved = _substitute_paths(settings_document, input_path=input_path, output_path=output_path)
    settings = load_settings_from_config_dict(resolved)
    bundle = instantiate_plugins_from_config(settings, preflight_mode=True, sink_effect_purpose=SinkEffectExecutionPurpose.FRESH)
    execution_sinks = execution_sinks_for_runtime(settings, bundle.sinks)
    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=execution_sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        coalesce_settings=list(settings.coalesce) if settings.coalesce else None,
        row_union_settings=list(settings.row_unions) if settings.row_unions else None,
        queues=settings.queues,
        collectors=bundle.collectors,
        scope_settings=list(settings.scopes) if settings.scopes else None,
        max_bound_region_depth=settings.max_bound_region_depth,
    )
    return graph, settings, bundle


@dataclass(frozen=True)
class _GroupLossRow:
    closer_name: str
    group_id: str
    member_key: str
    token_id: str
    reason: str
    loss_id: str
    recorded_at: Any


@dataclass
class PipelineResult:
    """Thin SQL-read probe over one run's audit DB (Task 9/9b's own style),
    plus the two extra facts this acceptance test needs: the run's overall
    counters (for the terminal-handling pins) and the EOF-flush iteration
    count captured live by the ``run_settings`` fixture's monkeypatch."""

    db_path: Path
    result_data: dict[str, Any]
    escalation_fixpoint_bound: int
    _flush_iterations_used: int

    def _connect(self):  # sqlalchemy Connection context manager, local test helper
        return create_engine(f"sqlite:///{self.db_path}").connect()

    def ledger(self) -> list[_GroupLossRow]:
        with self._connect() as conn:
            rows = conn.execute(select(group_losses_table)).mappings().all()
        return [
            _GroupLossRow(
                closer_name=row["closer_name"],
                group_id=row["group_id"],
                member_key=row["member_key"],
                token_id=row["token_id"],
                reason=row["reason"],
                loss_id=row["loss_id"],
                recorded_at=row["recorded_at"],
            )
            for row in rows
        ]

    def escalation_order(self, loss: _GroupLossRow) -> tuple[Any, str]:
        """Sort key: (recorded_at, loss_id) — the ledger's replay order."""
        return (loss.recorded_at, loss.loss_id)

    def non_terminal_tokens(self) -> list[str]:
        """Zero-write completeness: every minted token has a COMPLETED
        ``token_outcomes`` row by the time the run finishes."""
        with self._connect() as conn:
            minted = set(conn.execute(select(tokens_table.c.token_id)).scalars().all())
            completed = set(
                conn.execute(select(token_outcomes_table.c.token_id).where(token_outcomes_table.c.completed == 1)).scalars().all()
            )
        return sorted(minted - completed)

    def run_completed(self) -> bool:
        """The RUN reached a terminal, non-crashed status (COMPLETED,
        COMPLETED_WITH_FAILURES, FAILED, or EMPTY) — as opposed to RUNNING or
        INTERRUPTED. A data-level failure (the source row failing) is a
        DIFFERENT fact, checked by ``source_row_failed`` below; RunStatus.FAILED
        is itself a completed-without-crashing terminal status."""
        return self.result_data["status"] not in {"running", "interrupted"}

    def source_row_failed(self) -> bool:
        """The single source row's data outcome: nothing succeeded, at least
        one row-level failure was recorded — the outermost declared terminal
        handling (merge_1's own require_all failure) flagging the row failed
        at the run boundary rather than the run crashing."""
        return self.result_data["rows_succeeded"] == 0 and self.result_data["rows_failed"] >= 1

    def flush_iterations_used(self) -> int:
        return self._flush_iterations_used


@pytest.fixture
def run_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Real-executor harness (Task 9/9b style): builds + runs a settings
    DOCUMENT through the production path, counting EOF-flush intake
    iterations live via a ``RowProcessor.run_barrier_intake`` class-level
    monkeypatch (module docstring explains why this is an exact proxy and
    why a class-attribute patch, not a free-function patch, is required
    here)."""
    install_corpus_plugin_manager(monkeypatch)
    case_counter = itertools.count()

    intake_calls = {"count": 0}
    original_run_barrier_intake = RowProcessor.run_barrier_intake

    def _counting_run_barrier_intake(self: RowProcessor, ctx: Any) -> Any:
        intake_calls["count"] += 1
        return original_run_barrier_intake(self, ctx)

    monkeypatch.setattr(RowProcessor, "run_barrier_intake", _counting_run_barrier_intake)

    def _run(settings_document: dict[str, Any]) -> PipelineResult:
        intake_calls["count"] = 0
        case_dir = tmp_path / f"case-{next(case_counter)}"
        case_dir.mkdir()
        graph, settings, bundle = _build_graph_from_document(settings_document, case_dir)
        graph.validate()
        graph.validate_edge_compatibility()

        execution_sinks = execution_sinks_for_runtime(settings, bundle.sinks)
        execution_bindings = execution_sink_bindings_for_runtime(settings, bundle.sink_effect_bindings)
        sink_effect_modes = sink_effect_modes_from_runtime_bindings(
            execution_sinks,
            execution_bindings,
            purpose=SinkEffectExecutionPurpose.FRESH,
            configured_options={name: settings.sinks[name].options for name in execution_sinks},
        )
        sink_effect_admission = validate_pipeline_sink_effect_capabilities(
            execution_sinks,
            configured_modes=sink_effect_modes,
            required_input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
        )
        config = assemble_and_validate_pipeline_config(
            sources=bundle.sources,
            transforms=bundle.transforms,
            sinks=bundle.sinks,
            aggregations=bundle.aggregations,
            settings=settings,
            graph=graph,
            sink_effect_modes=sink_effect_modes,
            sink_effect_admission=sink_effect_admission,
        )

        catalog_sha256, catalog_source = read_openrouter_catalog_snapshot_id()
        db_path = case_dir / "audit.db"
        db = LandscapeDB(f"sqlite:///{db_path}")
        payload_store = FilesystemPayloadStore(case_dir / "payloads")
        try:
            result = Orchestrator(db).run(
                config,
                graph=graph,
                settings=settings,
                payload_store=payload_store,
                openrouter_catalog_sha256=catalog_sha256,
                openrouter_catalog_source=catalog_source,
            )
        finally:
            db.close()

        return PipelineResult(
            db_path=db_path,
            result_data=result.to_dict(),
            escalation_fixpoint_bound=graph.escalation_fixpoint_bound,
            _flush_iterations_used=intake_calls["count"],
        )

    return _run


@pytest.fixture
def build_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build-only harness (no run): returns the real production
    ``ExecutionGraph`` for a settings DOCUMENT, validated. Used by the
    depth-6 rejection test — the depth cap fires inside graph construction
    itself (``compute_bound_regions``, called from ``builder.py`` during
    ``ExecutionGraph.from_plugin_instances``), before ``.validate()`` ever
    runs."""
    install_corpus_plugin_manager(monkeypatch)
    case_counter = itertools.count()

    def _build(settings_document: dict[str, Any]) -> ExecutionGraph:
        case_dir = tmp_path / f"build-{next(case_counter)}"
        case_dir.mkdir()
        graph, _settings, _bundle = _build_graph_from_document(settings_document, case_dir)
        graph.validate()
        graph.validate_edge_compatibility()
        return graph

    return _build


def test_depth5_single_failure_unwraps_to_outermost_quarantine(run_settings) -> None:
    result = run_settings(_nested_settings(DEPTH))
    ledger = result.ledger()

    # One primary loss at layer 5.
    quarantined = [loss for loss in ledger if loss.reason == "quarantined"]
    assert len(quarantined) == 1
    assert quarantined[0].closer_name == f"merge_{DEPTH}"
    assert quarantined[0].member_key == f"go_{DEPTH}"

    # One escalated loss per enclosing layer 4..1 — structural pin (closer,
    # member) rather than a bare count, so a defect that fires the right
    # NUMBER of escalations at the WRONG frames still fails.
    escalated = [loss for loss in ledger if loss.reason == "group_failed"]
    assert len(escalated) == DEPTH - 1
    assert {(loss.closer_name, loss.member_key) for loss in escalated} == {(f"merge_{k}", f"go_{k}") for k in range(1, DEPTH)}
    # NOT pinned: strict escalation order via (recorded_at, loss_id).
    # Measured: multiple escalations settle within the same intake pass and
    # share the SAME `now` (the caller-supplied `record_group_loss` argument,
    # `group_losses.py:66`) with sub-second `DateTime` granularity, and
    # `loss_id` is `f"loss_{generate_id()[:12]}"` — not time-sortable. A
    # (recorded_at, loss_id) sort is therefore not a reliable proxy for
    # "one frame outward per level" and flakes on tie-breaking; the
    # structural (closer, member) set pin above is the load-bearing
    # assertion for the unwind shape. `escalation_order` is kept on
    # PipelineResult for a caller with a genuine ordering need (e.g. a
    # multi-source-row variant where cross-row interleaving matters).

    # Survivors ran to completion (no cancellation v1) and every token is
    # terminal — the zero-write direction checked by set equality.
    assert result.non_terminal_tokens() == []
    # Outermost declared terminal handling fired: the source row is flagged
    # failed at the run boundary, run itself completes (does not crash).
    assert result.run_completed()
    assert result.source_row_failed()


def test_depth6_is_rejected_at_build_without_override(build_graph) -> None:
    with pytest.raises(GraphValidationError, match=r"Bound-region nesting depth 6 exceeds the configured maximum 5"):
        build_graph(_nested_settings(6))


def test_flush_bound_scales_with_depth(run_settings) -> None:
    """The EOF fixpoint bound in force is the DERIVED value
    (``derive_escalation_fixpoint_bound(5) == 1_040``), not the superseded
    ``1_000`` constant — pinning only "converged under 1_040" would pass
    unchanged against a reverted hardcoded 1_000 (any converging run is also
    under 1_040); pinning the bound itself catches that regression."""
    result = run_settings(_nested_settings(DEPTH))
    assert derive_escalation_fixpoint_bound(DEPTH) == 1_040
    assert result.escalation_fixpoint_bound == 1_040
    assert result.flush_iterations_used() < result.escalation_fixpoint_bound
