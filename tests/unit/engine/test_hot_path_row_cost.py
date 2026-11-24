"""Exact production-path row-cost pins for SQLite and PostgreSQL.

The pins are equalities, including the fixed run cost. An intentional change to
the SQL or hashing path must remeasure and update the affected row in the same
change. A mismatch prints the full measured table for a re-pin.

SCOPE: Real orchestrator, scheduler, Landscape repositories, sink-effect
reservation/finalisation, checkpointing, and filesystem payload store. These are
main-thread call and SQL counts for fresh, uncontended, non-heartbeat runs under
a fixed clock: work on background threads (run heartbeat, sink heartbeat) is not
counted. Test fixtures supply sink admission and catalog defaults, so the fixed
term is not the CLI's bootstrap cost. Excludes real source decoding and concrete
sink I/O, configured telemetry exporters and event subscribers, retries,
error/quarantine rows, aggregation, resume, follower workers, external calls,
thresholds above six rows (64-drain maintenance and 500/900 chunk sizes), and
SQL planner complexity. The linearity oracle tests call-count growth at 2/4/6
rows, not elapsed cost or work inside a call. The four hash/JSON counters are
nested: a
core.canonical.stable_hash call implies downstream contracts calls; do not sum
them. SQL buckets are verb plus first table, localising a change to a bucket,
not one statement. Moving lines above a _KNOWN_SUPERLINEAR key requires a re-pin.
"""

from __future__ import annotations

import cProfile
import pstats
import re
import threading
import time
from collections import Counter
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection

import elspeth
import elspeth_lints
from elspeth.contracts import RunStatus, SourceRow
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.hashing import canonical_json as contracts_canonical_json
from elspeth.contracts.hashing import stable_hash as contracts_stable_hash
from elspeth.core.canonical import canonical_json as core_canonical_json
from elspeth.core.canonical import compute_full_topology_hash
from elspeth.core.canonical import stable_hash as core_stable_hash
from elspeth.core.checkpoint import CheckpointManager
from elspeth.core.checkpoint.compatibility import CheckpointCompatibilityValidator
from elspeth.core.config import CheckpointSettings, CoalesceSettings, ElspethSettings, GateSettings, SinkSettings, SourceSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.clock import MockClock
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from tests.fixtures.base_classes import as_sink, as_source, as_transform
from tests.fixtures.factories import wire_transforms
from tests.fixtures.plugins import CollectSink, ListSource, PassTransform

_SIZES = (2, 4, 6)  # Equal spacing exposes quadratic growth; below the scheduler's 64-drain maintenance period.
_HASH_DEFINITIONS = (
    (core_canonical_json, "core.canonical.canonical_json"),
    (core_stable_hash, "core.canonical.stable_hash"),
    (contracts_canonical_json, "contracts.hashing.canonical_json"),
    (contracts_stable_hash, "contracts.hashing.stable_hash"),
)
_HASH_FUNCTIONS = {
    (str(Path(function.__code__.co_filename).resolve()), function.__code__.co_firstlineno, function.__code__.co_name): name
    for function, name in _HASH_DEFINITIONS
}
_COUNTERS = ("statements", "commits", *(name for _, name in _HASH_DEFINITIONS))


# Each shape/dialect needs exact (fixed, per-row) integers for every counter.
# The SQL bucket mapping is also an exact pin, including fixed-only buckets.
def _bucket_pins(raw: str) -> dict[str, tuple[int, int]]:
    """Read the fixed and marginal integers in the reviewable bucket pin table."""
    pairs: dict[str, tuple[int, int]] = {}
    for line in raw.strip().splitlines():
        name, fixed, per_row = line.rsplit(" ", 2)
        assert name not in pairs, name
        pairs[name] = int(fixed), int(per_row)
    return pairs


_PINS: dict[str, dict[str, dict[str, object]]] = {
    "sqlite": {
        "linear": {
            "counters": {
                "statements": (586, 97),
                "commits": (67, 13),
                "core.canonical.canonical_json": (20, 14),
                "core.canonical.stable_hash": (16, 13),
                "contracts.hashing.canonical_json": (92, 45),
                "contracts.hashing.stable_hash": (29, 15),
            },
            "sql_buckets": _bucket_pins("""
BEGIN <expression> 75 13
DELETE checkpoints 1 0
INSERT artifacts 1 0
INSERT calls 3 0
INSERT checkpoints 1 1
INSERT edges 5 0
INSERT node_states 1 3
INSERT nodes 4 0
INSERT operations 2 0
INSERT routing_events 0 1
INSERT rows 0 1
INSERT run_coordination 1 0
INSERT run_coordination_events 4 0
INSERT run_sources 1 0
INSERT run_workers 1 0
INSERT runs 1 0
INSERT scheduler_events 0 4
INSERT sink_effect_attempts 3 0
INSERT sink_effect_members 1 0
INSERT sink_effect_streams 1 0
INSERT sink_effects 1 0
INSERT token_outcomes 1 0
INSERT token_work_items 0 1
INSERT tokens 0 1
PRAGMA foreign_keys 3 0
PRAGMA journal_mode 3 0
PRAGMA query_only 54 10
SELECT <expression> 129 11
SELECT artifacts 1 0
SELECT calls 3 0
SELECT checkpoints 1 1
SELECT collector_group_failures 1 0
SELECT node_states 6 9
SELECT nodes 5 1
SELECT operations 11 0
SELECT routing_events 0 4
SELECT rows 0 2
SELECT run_sources 3 0
SELECT run_workers 1 0
SELECT runs 4 0
SELECT sink_effect_attempts 26 0
SELECT sink_effect_members 9 0
SELECT sink_effect_streams 11 0
SELECT sink_effects 51 0
SELECT token_lineage_frames 1 1
SELECT token_outcomes 4 1
SELECT token_parents 0 1
SELECT token_work_items 5 7
SELECT tokens 2 4
UPDATE node_states 1 2
UPDATE nodes 1 0
UPDATE operations 2 0
UPDATE run_coordination 115 10
UPDATE run_sources 3 0
UPDATE run_workers 3 6
UPDATE runs 1 0
UPDATE sink_effect_attempts 3 0
UPDATE sink_effect_members 2 0
UPDATE sink_effect_streams 2 0
UPDATE sink_effects 10 0
UPDATE token_work_items 1 2
"""),
        },
        "fork": {
            "counters": {
                "statements": (621, 338),
                "commits": (70, 45),
                "core.canonical.canonical_json": (24, 36),
                "core.canonical.stable_hash": (20, 20),
                "contracts.hashing.canonical_json": (100, 75),
                "contracts.hashing.stable_hash": (33, 22),
            },
            "sql_buckets": _bucket_pins("""
BEGIN <expression> 83 53
DELETE checkpoints 1 0
INSERT artifacts 1 0
INSERT calls 3 0
INSERT checkpoints 1 1
INSERT coalesce_effect_members 0 1
INSERT coalesce_effects 0 1
INSERT edges 6 0
INSERT group_records 0 1
INSERT node_states 1 5
INSERT nodes 6 0
INSERT operations 2 0
INSERT routing_events 0 1
INSERT rows 0 1
INSERT run_coordination 1 0
INSERT run_coordination_events 4 0
INSERT run_sources 1 0
INSERT run_workers 1 0
INSERT runs 1 0
INSERT scheduler_events 0 13
INSERT sink_effect_attempts 3 0
INSERT sink_effect_members 1 0
INSERT sink_effect_streams 1 0
INSERT sink_effects 1 0
INSERT token_lineage_frames 0 1
INSERT token_outcomes 1 2
INSERT token_parents 0 2
INSERT token_work_items 0 3
INSERT tokens 0 3
PRAGMA foreign_keys 3 0
PRAGMA journal_mode 3 0
PRAGMA query_only 54 48
SELECT <expression> 138 40
SELECT artifacts 1 0
SELECT calls 3 0
SELECT checkpoints 1 1
SELECT coalesce_effect_members 0 1
SELECT coalesce_effects 0 2
SELECT collector_group_failures 1 0
SELECT group_losses 1 6
SELECT group_records 0 2
SELECT node_states 6 18
SELECT nodes 5 5
SELECT operations 11 0
SELECT routing_events 0 4
SELECT rows 0 8
SELECT run_sources 3 0
SELECT run_workers 1 2
SELECT runs 4 0
SELECT sink_effect_attempts 26 0
SELECT sink_effect_members 9 0
SELECT sink_effect_streams 11 0
SELECT sink_effects 51 0
SELECT token_lineage_frames 1 8
SELECT token_outcomes 4 4
SELECT token_parents 1 4
SELECT token_work_items 9 26
SELECT tokens 2 17
UPDATE coalesce_effects 0 1
UPDATE node_states 1 3
UPDATE nodes 1 0
UPDATE operations 2 0
UPDATE run_coordination 124 25
UPDATE run_sources 3 0
UPDATE run_workers 3 14
UPDATE runs 1 0
UPDATE sink_effect_attempts 3 0
UPDATE sink_effect_members 2 0
UPDATE sink_effect_streams 2 0
UPDATE sink_effects 10 0
UPDATE token_work_items 1 11
"""),
        },
    },
    "postgres": {
        "linear": {
            "counters": {
                "statements": (501, 79),
                "commits": (67, 13),
                "core.canonical.canonical_json": (20, 14),
                "core.canonical.stable_hash": (16, 14),
                "contracts.hashing.canonical_json": (92, 45),
                "contracts.hashing.stable_hash": (29, 15),
            },
            "sql_buckets": {
                "DELETE checkpoints": (1, 0),
                "INSERT artifacts": (1, 0),
                "INSERT calls": (3, 0),
                "INSERT checkpoints": (1, 1),
                "INSERT edges": (5, 0),
                "INSERT node_states": (1, 3),
                "INSERT nodes": (4, 0),
                "INSERT operations": (2, 0),
                "INSERT routing_events": (0, 1),
                "INSERT rows": (0, 1),
                "INSERT run_coordination": (1, 0),
                "INSERT run_coordination_events": (4, 0),
                "INSERT run_sources": (1, 0),
                "INSERT run_workers": (1, 0),
                "INSERT runs": (1, 0),
                "INSERT scheduler_events": (1, 3),
                "INSERT sink_effect_attempts": (3, 0),
                "INSERT sink_effect_members": (1, 0),
                "INSERT sink_effect_streams": (1, 0),
                "INSERT sink_effects": (1, 0),
                "INSERT token_outcomes": (1, 0),
                "INSERT token_work_items": (0, 1),
                "INSERT tokens": (0, 1),
                "SELECT <expression>": (148, 12),
                "SELECT artifacts": (1, 0),
                "SELECT calls": (3, 0),
                "SELECT checkpoints": (1, 1),
                "SELECT collector_group_failures": (1, 0),
                "SELECT node_states": (7, 9),
                "SELECT nodes": (5, 1),
                "SELECT operations": (11, 0),
                "SELECT routing_events": (0, 4),
                "SELECT rows": (0, 2),
                "SELECT run_sources": (3, 0),
                "SELECT run_workers": (1, 0),
                "SELECT runs": (4, 0),
                "SELECT sink_effect_attempts": (26, 0),
                "SELECT sink_effect_members": (9, 0),
                "SELECT sink_effect_streams": (11, 0),
                "SELECT sink_effects": (51, 0),
                "SELECT token_lineage_frames": (1, 1),
                "SELECT token_outcomes": (4, 1),
                "SELECT token_parents": (0, 1),
                "SELECT token_work_items": (5, 7),
                "SELECT tokens": (3, 4),
                "SET <expression>": (28, 5),
                "UPDATE node_states": (1, 2),
                "UPDATE nodes": (1, 0),
                "UPDATE operations": (2, 0),
                "UPDATE run_coordination": (115, 10),
                "UPDATE run_sources": (3, 0),
                "UPDATE run_workers": (3, 6),
                "UPDATE runs": (1, 0),
                "UPDATE sink_effect_attempts": (3, 0),
                "UPDATE sink_effect_members": (2, 0),
                "UPDATE sink_effect_streams": (2, 0),
                "UPDATE sink_effects": (10, 0),
                "UPDATE token_work_items": (1, 2),
            },
        },
        "fork": {
            "counters": {
                "statements": (528, 263),
                "commits": (70, 45),
                "core.canonical.canonical_json": (24, 36),
                "core.canonical.stable_hash": (20, 21),
                "contracts.hashing.canonical_json": (100, 75),
                "contracts.hashing.stable_hash": (33, 22),
            },
            "sql_buckets": {
                "DELETE checkpoints": (1, 0),
                "INSERT artifacts": (1, 0),
                "INSERT calls": (3, 0),
                "INSERT checkpoints": (1, 1),
                "INSERT coalesce_effect_members": (0, 1),
                "INSERT coalesce_effects": (0, 1),
                "INSERT edges": (6, 0),
                "INSERT group_records": (0, 1),
                "INSERT node_states": (1, 5),
                "INSERT nodes": (6, 0),
                "INSERT operations": (2, 0),
                "INSERT routing_events": (0, 1),
                "INSERT rows": (0, 1),
                "INSERT run_coordination": (1, 0),
                "INSERT run_coordination_events": (4, 0),
                "INSERT run_sources": (1, 0),
                "INSERT run_workers": (1, 0),
                "INSERT runs": (1, 0),
                "INSERT scheduler_events": (1, 10),
                "INSERT sink_effect_attempts": (3, 0),
                "INSERT sink_effect_members": (1, 0),
                "INSERT sink_effect_streams": (1, 0),
                "INSERT sink_effects": (1, 0),
                "INSERT token_lineage_frames": (0, 1),
                "INSERT token_outcomes": (1, 2),
                "INSERT token_parents": (0, 2),
                "INSERT token_work_items": (0, 3),
                "INSERT tokens": (0, 3),
                "SELECT <expression>": (157, 41),
                "SELECT artifacts": (1, 0),
                "SELECT calls": (3, 0),
                "SELECT checkpoints": (1, 1),
                "SELECT coalesce_effect_members": (0, 1),
                "SELECT coalesce_effects": (0, 2),
                "SELECT collector_group_failures": (1, 0),
                "SELECT group_losses": (1, 6),
                "SELECT group_records": (0, 2),
                "SELECT node_states": (7, 19),
                "SELECT nodes": (5, 5),
                "SELECT operations": (11, 0),
                "SELECT routing_events": (0, 4),
                "SELECT rows": (0, 8),
                "SELECT run_sources": (3, 0),
                "SELECT run_workers": (1, 2),
                "SELECT runs": (4, 0),
                "SELECT sink_effect_attempts": (26, 0),
                "SELECT sink_effect_members": (9, 0),
                "SELECT sink_effect_streams": (11, 0),
                "SELECT sink_effects": (51, 0),
                "SELECT token_lineage_frames": (1, 8),
                "SELECT token_outcomes": (4, 4),
                "SELECT token_parents": (1, 4),
                "SELECT token_work_items": (9, 26),
                "SELECT tokens": (3, 20),
                "SET <expression>": (28, 24),
                "UPDATE coalesce_effects": (0, 1),
                "UPDATE node_states": (1, 3),
                "UPDATE nodes": (1, 0),
                "UPDATE operations": (2, 0),
                "UPDATE run_coordination": (124, 25),
                "UPDATE run_sources": (3, 0),
                "UPDATE run_workers": (3, 14),
                "UPDATE runs": (1, 0),
                "UPDATE sink_effect_attempts": (3, 0),
                "UPDATE sink_effect_members": (2, 0),
                "UPDATE sink_effect_streams": (2, 0),
                "UPDATE sink_effects": (10, 0),
                "UPDATE token_work_items": (1, 11),
            },
        },
    },
}

# No known super-linear Python call sites remain. E-11 is a quadratic SQL
# query plan, which this call-count oracle cannot detect.
_KNOWN_SUPERLINEAR = frozenset()


class _SharedContractListSource(ListSource):
    """Reuse one contract as CSV, JSON, and Text sources do for stable fields.

    See csv_source.py, json_source.py, and text_source.py. This models the
    default source path only. With ``snapshot_for_resume`` enabled the engine
    decodes a contract per row (source_snapshot.py), and a JSON source whose
    later rows add fields changes its contract; these pins cover neither.
    """

    def load(self, ctx: Any) -> Iterator[SourceRow]:
        if not self._data:
            return
        yield from self.wrap_rows(self._data[:1])
        contract = self._schema_contract
        assert contract is not None
        for source_row_index, row in enumerate(self._data[1:], start=1):
            yield SourceRow.valid(row, contract=contract, source_row_index=source_row_index)


def _sql_bucket(statement: str) -> str:
    """Group an execution by leading SQL verb and first target table."""
    sql = statement.strip()
    verb_match = re.match(r"([A-Za-z]+)", sql)
    if verb_match is None:
        return "<unknown> <expression>"
    verb = verb_match.group(1).upper()
    target_pattern = {
        "SELECT": r"\bFROM\s+([\w.\"]+)",
        "UPDATE": r"^UPDATE\s+([\w.\"]+)",
        "INSERT": r"^INSERT\s+INTO\s+([\w.\"]+)",
        "DELETE": r"^DELETE\s+FROM\s+([\w.\"]+)",
        "PRAGMA": r"^PRAGMA\s+([\w.\"]+)",
    }.get(verb)
    target = re.search(target_pattern, sql, re.IGNORECASE) if target_pattern is not None else None
    return f"{verb} {target.group(1).strip(chr(34)) if target is not None else '<expression>'}"


def _pipeline(shape: str, rows: int, db: LandscapeDB, root: Path) -> None:
    source = _SharedContractListSource([{"value": index} for index in range(rows)], on_success="gate_in")
    source_settings = SourceSettings(plugin="list_source", on_success="gate_in", options={})
    output = CollectSink(name="output")
    transform = PassTransform()
    checkpoint_settings = CheckpointSettings(enabled=True, frequency="every_row")

    if shape == "linear":
        gate = GateSettings(
            name="linear_gate",
            input="gate_in",
            condition="True",
            routes={"true": "transform_in", "false": "output"},
        )
        wired = wire_transforms([as_transform(transform)], source_connection="transform_in", final_sink="output")
        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(transform)],
            sinks={"output": as_sink(output)},
            gates=[gate],
        )
        graph = ExecutionGraph.from_plugin_instances(
            sources=config.sources,
            source_settings_map={"primary": source_settings},
            transforms=wired,
            sinks=config.sinks,
            gates=[gate],
        )
        settings = ElspethSettings(
            sources={"primary": source_settings},
            sinks={"output": SinkSettings(plugin="collect", on_write_failure="discard", options={})},
            gates=[gate],
            checkpoint=checkpoint_settings,
        )
    else:
        gate = GateSettings(
            name="fork_gate",
            input="gate_in",
            condition="True",
            routes={"true": "fork", "false": "output"},
            fork_to=["path_a", "path_b"],
        )
        coalesce = CoalesceSettings(
            name="merge_paths",
            branches={"path_a": "path_a", "path_b": "path_b"},
            policy="require_all",
            merge="union",
            on_success="output",
        )
        # The second source sink mirrors the established nonterminal coalesce
        # fixture and preserves the fork workload's final routing shape.
        source_sink = CollectSink(name="source_sink")
        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(transform)],
            sinks={"output": as_sink(output), "source_sink": as_sink(source_sink)},
            gates=[gate],
            coalesce_settings=[coalesce],
        )
        wired = wire_transforms([as_transform(transform)], source_connection="source_out", final_sink="gate_in")
        source.on_success = "source_out"
        source_settings = SourceSettings(plugin="list_source", on_success="source_out", options={})
        graph = ExecutionGraph.from_plugin_instances(
            sources=config.sources,
            source_settings_map={"primary": source_settings},
            transforms=wired,
            sinks=config.sinks,
            gates=[gate],
            coalesce_settings=[coalesce],
        )
        settings = ElspethSettings(
            sources={"primary": source_settings},
            sinks={
                "output": SinkSettings(plugin="collect", on_write_failure="discard", options={}),
                "source_sink": SinkSettings(plugin="collect", on_write_failure="discard", options={}),
            },
            gates=[gate],
            coalesce=[coalesce],
            checkpoint=checkpoint_settings,
        )

    checkpoint_config = RuntimeCheckpointConfig.from_settings(settings.checkpoint)
    checkpoint_manager = CheckpointManager(db) if checkpoint_config.enabled else None
    orchestrator = Orchestrator(
        db,
        checkpoint_manager=checkpoint_manager,
        checkpoint_config=checkpoint_config,
        clock=MockClock(start=time.time()),
    )
    result = orchestrator.run(config, graph=graph, settings=settings, payload_store=FilesystemPayloadStore(root / "payloads"))
    assert result.status == RunStatus.COMPLETED
    assert result.rows_processed == rows
    assert len(output.results) == rows


def _profiled_run(shape: str, rows: int, db: LandscapeDB, root: Path) -> tuple[Counter[str], dict[str, int]]:
    counts: Counter[str] = Counter()
    calls: dict[str, int] = {}
    main_thread = threading.get_ident()

    def before_cursor_execute(
        _connection: Connection, _cursor: Any, statement: str, _parameters: Any, _context: Any, _executemany: bool
    ) -> None:
        if threading.get_ident() == main_thread:
            counts["statements"] += 1
            counts[f"sql:{_sql_bucket(statement)}"] += 1

    def on_commit(_connection: Connection) -> None:
        if threading.get_ident() == main_thread:
            counts["commits"] += 1

    engine = db.engine
    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    event.listen(engine, "commit", on_commit)
    try:
        profiler = cProfile.Profile()
        profiler.enable()
        try:
            _pipeline(shape, rows, db, root)
        finally:
            profiler.disable()
        package_dir = Path(elspeth.__file__).resolve().parent
        for (filename, line, name), (_primitive, calls_total, _own_time, _cumulative, _callers) in pstats.Stats(profiler).stats.items():
            path = Path(filename).resolve()
            if not path.is_relative_to(package_dir):
                continue
            relative = path.relative_to(package_dir).as_posix()
            calls[f"{relative}:{line}:{name}"] = calls_total
            counter = _HASH_FUNCTIONS.get((str(path), line, name))
            if counter is not None:
                counts[counter] += calls_total
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
        event.remove(engine, "commit", on_commit)
    return counts, calls


def _measure(shape: str, root: Path, postgres_url: str | None) -> dict[int, tuple[Counter[str], dict[str, int]]]:
    worktree = Path(__file__).resolve().parents[3]
    assert Path(elspeth.__file__).resolve().is_relative_to(worktree), elspeth.__file__
    assert Path(elspeth_lints.__file__).resolve().is_relative_to(worktree), elspeth_lints.__file__
    measured: dict[int, tuple[Counter[str], dict[str, int]]] = {}
    for rows in (1, *_SIZES):
        url = postgres_url if postgres_url is not None else f"sqlite:///{root / f'audit-{shape}-{rows}.db'}"
        assert url is not None
        db = LandscapeDB.from_url(url)
        try:
            sample = _profiled_run(shape, rows, db, root / f"payload-{shape}-{rows}")
        finally:
            db.close()
        if rows in _SIZES:
            measured[rows] = sample
    assert set(measured) == set(_SIZES)
    return measured


def _fit(values: Mapping[int, int]) -> tuple[int, int]:
    low, middle, high = _SIZES
    increase = values[middle] - values[low]
    step = middle - low
    assert increase % step == 0, f"nonintegral per-row marginal: {dict(values)}"
    per_row = increase // step
    assert values[high] == values[middle] + per_row * (high - middle), f"nonlinear count: {dict(values)}"
    return values[low] - per_row * low, per_row


def _oracle_violations(profiles: Mapping[int, Mapping[str, int]]) -> tuple[set[str], set[str]]:
    low, middle, high = _SIZES
    offenders: set[str] = set()
    for function in set().union(*(set(profile) for profile in profiles.values())):
        values = {size: profiles[size].get(function, 0) for size in _SIZES}
        if values[high] - values[middle] > values[middle] - values[low]:
            offenders.add(function)
    return offenders - _KNOWN_SUPERLINEAR, _KNOWN_SUPERLINEAR - offenders


def _assert_oracle(profiles: Mapping[int, Mapping[str, int]], table: str) -> None:
    unexpected, stale = _oracle_violations(profiles)
    assert not unexpected, f"superlinear elspeth function(s): {sorted(unexpected)}\n{table}"
    assert not stale, f"stale L3-1 known-superlinear entry: {sorted(stale)}\n{table}"


def _table(
    measured: Mapping[int, tuple[Counter[str], dict[str, int]]],
) -> tuple[str, dict[str, tuple[int, int]], dict[str, tuple[int, int]], list[str]]:
    count_keys = set(_COUNTERS)
    bucket_keys = set().union(*(set(sample[0]) for sample in measured.values())) - count_keys
    rows: list[str] = ["counter                         n2      n4      n6    fixed  per-row"]
    counters: dict[str, tuple[int, int]] = {}
    buckets: dict[str, tuple[int, int]] = {}
    errors: list[str] = []
    for key in (*_COUNTERS, *sorted(bucket_keys)):
        values = {size: measured[size][0][key] for size in _SIZES}
        try:
            fixed, marginal = _fit(values)
        except AssertionError as exc:
            fixed, marginal = 0, 0
            errors.append(f"{key}: {exc}")
        rows.append(f"{key:31} {values[2]:7} {values[4]:7} {values[6]:7} {fixed:8} {marginal:8}")
        if key in count_keys:
            counters[key] = fixed, marginal
        else:
            buckets[key.removeprefix("sql:")] = fixed, marginal
    if errors:
        rows.extend(("nonlinear or nonintegral:", *errors))
    return "\n".join(rows), counters, buckets, errors


def _assert_measurement(shape: str, dialect: str, measured: Mapping[int, tuple[Counter[str], dict[str, int]]]) -> None:
    table, counters, buckets, errors = _table(measured)
    profiles = {size: sample[1] for size, sample in measured.items()}
    pin = _PINS[dialect][shape]
    assert not errors, table
    assert sum(value[0] for value in buckets.values()) == counters["statements"][0], table
    assert sum(value[1] for value in buckets.values()) == counters["statements"][1], table
    _assert_oracle(profiles, table)
    payload_store_key = f"core/payload_store.py:{FilesystemPayloadStore.store.__code__.co_firstlineno}:store"
    payload_stores = {size: profiles[size].get(payload_store_key, 0) for size in _SIZES}
    assert _fit(payload_stores) == ((1, 3) if shape == "linear" else (1, 4)), f"payload_store={payload_stores!r}\n{table}"
    assert counters == pin["counters"] and buckets == pin["sql_buckets"], (
        f"{dialect}/{shape} row-cost pin mismatch; remeasure and pin these exact pairs:\n"
        f"counters={counters!r}\nsql_buckets={buckets!r}\n{table}"
    )


@pytest.mark.parametrize("shape", ("linear", "fork"))
def test_sqlite_row_cost_pins(shape: str, tmp_path: Path) -> None:
    measured = _measure(shape, tmp_path, None)
    _assert_measurement(shape, "sqlite", measured)


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    from tests.helpers.postgres_target import postgres_test_target

    with postgres_test_target(driver="psycopg") as url:
        yield url


@pytest.mark.testcontainer
@pytest.mark.parametrize("shape", ("linear", "fork"))
def test_postgres_row_cost_pins(shape: str, tmp_path: Path, postgres_url: str) -> None:
    measured = _measure(shape, tmp_path, postgres_url)
    _assert_measurement(shape, "postgres", measured)


def test_oracle_names_a_quadratic_function() -> None:
    function = "synthetic/quadratic.py:1:helper"
    profiles = {size: {function: size * size} for size in _SIZES}
    with pytest.raises(AssertionError, match=r"synthetic/quadratic\.py:1:helper"):
        _assert_oracle(profiles, "synthetic measurement")


@pytest.mark.parametrize("shape", ("linear", "fork"))
def test_topology_hash_computed_once_for_fifty_checkpoints(shape: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 50-row every-row run hashes its topology once and stores that digest 51 times.

    E-9a (maintainer ruling 2026-11-20: the checkpoint topology hash is fixed
    at run start). The expected digest is recomputed independently after the
    run, so the only computation counted is the one the coordinator made.
    """
    calls: list[str] = []
    stored: list[str] = []
    graphs: list[ExecutionGraph] = []
    original_hash = CheckpointCompatibilityValidator.compute_full_topology_hash
    original_create = CheckpointManager.create_checkpoint

    def count_hash(self: CheckpointCompatibilityValidator, graph: ExecutionGraph) -> str:
        digest = original_hash(self, graph)
        calls.append(digest)
        graphs.append(graph)
        return digest

    def record_checkpoint(self: CheckpointManager, *, draft: Any, coordination_token: Any) -> Any:
        stored.append(draft.upstream_topology_hash)
        return original_create(self, draft=draft, coordination_token=coordination_token)

    monkeypatch.setattr(CheckpointCompatibilityValidator, "compute_full_topology_hash", count_hash)
    monkeypatch.setattr(CheckpointManager, "create_checkpoint", record_checkpoint)
    db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'audit.db'}")
    try:
        _pipeline(shape, 50, db, tmp_path)
    finally:
        db.close()

    assert graphs
    expected = compute_full_topology_hash(graphs[0])
    assert len(stored) == 51
    assert calls == [expected]
    assert set(stored) == {expected}


def test_distinct_runs_do_not_share_topology_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two runs with different topologies in one process store their own baseline digests.

    The cached hash belongs to one coordinator's installed graph, not to the
    process: a module-level or graph-shared cache would hand the second run
    the first run's digest.
    """
    hashes: list[str] = []
    original_create = CheckpointManager.create_checkpoint

    def record_checkpoint(self: CheckpointManager, *, draft: Any, coordination_token: Any) -> Any:
        if draft.sequence_number == 0:
            hashes.append(draft.upstream_topology_hash)
        return original_create(self, draft=draft, coordination_token=coordination_token)

    monkeypatch.setattr(CheckpointManager, "create_checkpoint", record_checkpoint)
    for shape in ("linear", "fork"):
        db = LandscapeDB.from_url(f"sqlite:///{tmp_path / f'{shape}.db'}")
        try:
            _pipeline(shape, 2, db, tmp_path / shape)
        finally:
            db.close()
    assert len(hashes) == 2
    assert hashes[0] != hashes[1]
