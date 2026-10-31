"""Observe both retention roots, with a separately bounded legacy DFS oracle.

This replaces the handover instrument's source patching and coordinate-only
keys with cache observation and per-unit AST occurrence identities. Capped
oracle roots are explicitly un-compared. A clean partial result is exit 2,
never a claim of complete DFS equivalence. Any mismatch is exit 1.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any


class _OracleBudgetExceeded(BaseException):
    pass


class _OracleFrames(list[object]):
    def __init__(self, budget: int) -> None:
        super().__init__()
        self.budget = budget
        self.evaluations = 0

    def append(self, value: object) -> None:
        self.evaluations += 1
        if self.evaluations > self.budget:
            raise _OracleBudgetExceeded
        super().append(value)


def _origins(origins: Iterable[tuple[str, str] | None]) -> list[object]:
    return sorted((list(origin) if origin is not None else None for origin in origins), key=repr)


def _key(record: dict[str, Any]) -> tuple[object, ...]:
    return tuple(record["key"])


def index_records(records: list[dict[str, Any]]) -> dict[tuple[object, ...], dict[str, Any]]:
    result = {}
    for record in records:
        key = _key(record)
        if key in result:
            raise ValueError(f"duplicate retention root key: {key}")
        result[key] = record
    return result


def validate(result: dict[str, Any]) -> list[str]:
    records = index_records(result["records"])
    errors = []
    for key, record in records.items():
        state = record["state"]
        old = record["oracle"]
        if old is None:
            continue
        if state["acyclic"]:
            if state["origins"] != [old["origin"]] or state["retained"] != old["retained"]:
                errors.append(f"acyclic R/O mismatch: {key}")
        elif old["origin"] not in state["origins"] or (old["retained"] and not state["retained"]):
            errors.append(f"cyclic R/O inclusion failure: {key}")
    return errors


def compare(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    """Fail on missing roots, duplicate roots, changed inputs or changed answers."""
    a, b = index_records(left["records"]), index_records(right["records"])
    errors = validate(left) + validate(right)
    if left["sources"] != right["sources"]:
        errors.append("source digest inventory differs")
    if a.keys() != b.keys():
        errors.append(f"root inventory differs: left-only={sorted(a.keys() - b.keys())} right-only={sorted(b.keys() - a.keys())}")
    for key in a.keys() & b.keys():
        if a[key]["state"] != b[key]["state"]:
            errors.append(f"graph state differs: {key}")
    if set(left["failures"]) != set(right["failures"]):
        errors.append("failure row sets differ")
    return errors


def _live_sources(gate: Any, tree: Path, paths: tuple[str, ...]) -> dict[str, str]:
    """Hash the gate's UTF-8/universal-newline text policy, not unrelated bytes."""
    result = {}
    for path in gate.iter_gate_files(tree / "src" / "elspeth"):
        relative = path.relative_to(tree).as_posix()
        if paths and relative not in paths:
            continue
        if relative in result:
            raise ValueError(f"duplicate live source path: {relative}")
        result[relative] = hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    return result


def load_gate(tree: Path) -> Any:
    """Import the inventory loader only after measure has frozen its bytes."""
    from scripts.fencing_inventory import load_gate as loader

    expected = Path(__file__).with_name("fencing_inventory.py").resolve()
    if Path(loader.__code__.co_filename).resolve() != expected:
        raise ValueError("inventory loader imported from another checkout")
    return loader(tree)


def _proof_hashes(paths: dict[str, Path]) -> dict[str, str]:
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}


class _LegacyConsumerIncomplete(BaseException):
    pass


def measure(
    tree: Path,
    oracle_budget: int,
    *,
    reverse: bool = False,
    paths: tuple[str, ...] = (),
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    wall_started = time.monotonic()

    def emit(event: str, **fields: Any) -> None:
        if progress is not None:
            progress({"event": event, "elapsed_s": time.monotonic() - wall_started, **fields})

    # Known filenames are frozen before the loader, gate or graph helper is
    # imported/executed. The surrounding immutable-process receipt binds this
    # instrument's own already-running module to its reviewed source bytes.
    proof_paths = {
        "gate": tree / "tests/unit/architecture/test_web_landscape_mutation_fencing.py",
        "graph": tree / "tests/helpers/web_reserve_retention.py",
        "inventory_loader": Path(__file__).with_name("fencing_inventory.py"),
        "instrument": Path(__file__),
    }
    proof_before = _proof_hashes(proof_paths)
    emit("proof_import_start")
    gate: Any = load_gate(tree)
    if _proof_hashes(proof_paths) != proof_before:
        raise ValueError("proof inputs changed across gate import")
    if Path(gate.__file__).resolve() != proof_paths["gate"].resolve():
        raise ValueError("gate imported from another checkout")
    if Path(gate._reserve_retention.__file__).resolve() != proof_paths["graph"].resolve():
        raise ValueError("graph helper imported from another checkout")
    before_sources = _live_sources(gate, tree, paths)
    units = tuple(unit for unit in gate._production_units() if not paths or unit.path in paths)
    if not units:
        raise ValueError("empty production source inventory")
    if paths and set(paths) != {unit.path for unit in units}:
        raise ValueError("a requested source path was not enumerated by the production-unit authority")
    sources = {}
    for unit in units:
        if unit.path in sources:
            raise ValueError(f"duplicate parsed source path: {unit.path}")
        sources[unit.path] = hashlib.sha256(unit.source.encode("utf-8")).hexdigest()
    if sources != before_sources or _live_sources(gate, tree, paths) != sources:
        raise ValueError("parsed source differs from the frozen live source inventory")
    emit("source_inventory_frozen", units=len(units))
    walked = {unit.path: tuple(ast.walk(unit.tree)) for unit in units}
    ordinals = {path: {node: ordinal for ordinal, node in enumerate(nodes)} for path, nodes in walked.items()}
    records: dict[tuple[str, int, bool, bool], dict[str, Any]] = {}
    original_cache = gate.cache
    original_combinations: Callable[..., tuple[str, ...]] = gate._reserve_retention.consumer_combinations
    graph_budget_trips: set[tuple[str, int, bool, bool]] = set()
    combination_attempts: Counter[int] = Counter()
    consumer_work: Counter[int] = Counter()
    visits = 0
    phase = {"legacy": False}
    context: dict[str, Any] = {"unit": None, "site": None}
    consumer_counts: dict[bool, Counter[str]] = {False: Counter(), True: Counter()}
    cyclic_sites: set[tuple[str, int]] = set()
    incomplete_sites: set[tuple[str, int]] = set()
    graph_rows: dict[tuple[str, int], tuple[str, ...]] = {}
    legacy_rows: dict[tuple[str, int], tuple[str, ...]] = {}

    def finish_unit() -> None:
        if context.get("progress_unit") is not None:
            graph = context["progress_graph"]
            emit(
                "unit_finished",
                path=context["progress_unit"],
                legacy=context["progress_legacy"],
                graph_evaluations=graph.evaluations,
                final_keys=len(graph.final),
                records=len(records),
                incomplete_consumers=len(incomplete_sites),
            )
            context["progress_unit"] = None

    def observed_cache(function: Any) -> Any:
        wrapped = original_cache(function)
        expression_root = function.__qualname__.endswith(".retained_empty_root_state")
        capture_root = function.__qualname__.endswith(".retained_capture_root")
        if not (expression_root or capture_root):
            return wrapped
        evaluate = function.__kwdefaults__["evaluate"]
        unit = evaluate.__kwdefaults__["unit"]
        graph = evaluate.__kwdefaults__["graph"]
        graph.reverse = reverse
        if expression_root:
            finish_unit()
            context.update(progress_unit=unit.path, progress_graph=graph, progress_legacy=phase["legacy"])
            emit("unit_started", path=unit.path, legacy=phase["legacy"])
        context["unit"] = unit
        reference = function.__kwdefaults__["reference"]
        reference_state = evaluate.__kwdefaults__["reference"]
        memo = reference_state.__kwdefaults__["memo"]
        root_budget = reference_state.__kwdefaults__["retained_root_budget"]
        legacy = phase["legacy"]

        def bounded_oracle(value: Any, read_target: bool) -> tuple[dict[str, Any] | None, int]:
            old_budget = gate._RETENTION_ROOT_EVALUATION_BUDGET
            old_frames = memo["traces"]
            assert old_frames == []
            old_evaluations = root_budget["evaluations"]
            frames = _OracleFrames(oracle_budget)
            memo["traces"] = frames
            gate._RETENTION_ROOT_EVALUATION_BUDGET = None
            root_budget["evaluations"] = 0
            try:
                old_result = reference(value) if capture_root else reference(value, frozenset(), read_target=read_target)
            except _OracleBudgetExceeded:
                old = None
            else:
                old = (
                    {"origin": None, "retained": old_result}
                    if capture_root
                    else {
                        "origin": list(old_result[0]) if old_result[0] is not None else None,
                        "retained": old_result[1],
                    }
                )
            finally:
                gate._RETENTION_ROOT_EVALUATION_BUDGET = old_budget
                root_budget["evaluations"] = old_evaluations
                memo["traces"] = old_frames
                assert frames == []
            return old, frames.evaluations

        def current(value: ast.expr | ast.stmt, **kwargs: Any) -> Any:
            nonlocal visits
            read_target = kwargs.get("read_target", False)
            key = (unit.path, ordinals[unit.path][value], read_target, capture_root)
            if legacy:
                old, _evaluations = bounded_oracle(value, read_target)
                if old is None:
                    raise _LegacyConsumerIncomplete
                if capture_root:
                    return old["retained"]
                origin = tuple(old["origin"]) if old["origin"] is not None else None
                return gate._reserve_retention.RetentionState(frozenset({origin}), old["retained"], True)
            visits += 1
            before = graph.evaluations
            try:
                result = wrapped(value, **kwargs)
            except (gate._RetentionBudgetExceeded, gate._RetentionConsumerBudgetExceeded):
                graph_budget_trips.add(key)
                raise
            state = graph.final[value, read_target, capture_root]
            if context["site"] is None:
                raise ValueError("retention root executed outside the observed consumer inventory")
            if not state.acyclic:
                cyclic_sites.add(context["site"])
            encoded = {"origins": _origins(state.origins), "retained": state.retained, "acyclic": state.acyclic}
            if key in records:
                if records[key]["state"] != encoded:
                    raise ValueError(f"repeated graph root changed answer: {key}")
                records[key]["visits"] += 1
                return result
            old, evaluations = bounded_oracle(value, read_target)
            records[key] = {
                "key": list(key),
                "line": value.lineno,
                "column": value.col_offset,
                "state": encoded,
                "oracle": old,
                "oracle_evaluations": evaluations,
                "graph_evaluations": graph.evaluations - before,
                "visits": 1,
            }
            return result

        return original_cache(current) if legacy else current

    def observed_combinations(consumer: Any, expressions: Any, charge: Callable[[], None]) -> tuple[str, ...]:
        legacy = phase["legacy"]
        unit = context["unit"]
        node = consumer.__defaults__[0]
        ordinal = consumer_counts[legacy][unit.path]
        # Context/operator AST objects may be shared by several occurrences.
        # Their ordinal comes from the actual walk position, never a dict that
        # overwrites earlier occurrences of the same singleton object.
        if ordinal >= len(walked[unit.path]) or walked[unit.path][ordinal] is not node:
            raise ValueError(f"missing, duplicate or reordered consumer: {unit.path}:{ordinal}")
        consumer_counts[legacy][unit.path] += 1
        site = (unit.path, ordinal)
        context["site"] = site
        attempts = 0

        def counted_charge() -> None:
            nonlocal attempts
            attempts += 1
            charge()

        try:
            reasons = original_combinations(consumer, expressions, counted_charge)
        except _LegacyConsumerIncomplete:
            if not legacy:
                raise
            incomplete_sites.add(site)
            reasons = ()
        finally:
            if not legacy:
                combination_attempts[attempts] += 1
                defaults = charge.__kwdefaults__
                assert defaults is not None
                consumer_work[defaults["retained_root_budget"]["consumer_evaluations"]] += 1
            context["site"] = None
        if reasons:
            line = consumer.__defaults__[0].lineno
            rows = tuple(reason + unit.path + ":" + str(line) for reason in reasons)
            (legacy_rows if legacy else graph_rows)[site] = rows
        return reasons

    gate.cache = observed_cache
    gate._reserve_retention.consumer_combinations = observed_combinations
    started = time.process_time()
    try:
        emit("pass_started", legacy=False)
        failures = list(gate._reserve_selected_origin_effect_failures(units))
        finish_unit()
        emit("pass_finished", legacy=False, failures=len(failures), roots=len(records))
        phase["legacy"] = True
        emit("pass_started", legacy=True)
        legacy_failures = list(gate._reserve_selected_origin_effect_failures(units))
        finish_unit()
        emit("pass_finished", legacy=True, failures=len(legacy_failures), incomplete_consumers=len(incomplete_sites))
    finally:
        # Completed units are reported above, before cleanup. An interrupted
        # unit remains unfinished; optional progress I/O must neither prevent
        # restoration nor replace the exception that interrupted analysis.
        gate.cache = original_cache
        gate._reserve_retention.consumer_combinations = original_combinations
    for legacy, counts in consumer_counts.items():
        if dict(counts) != {unit.path: len(walked[unit.path]) for unit in units}:
            raise ValueError(f"incomplete consumer inventory in legacy={legacy}: {dict(counts)}")
    if not records or not any(key[-1] for key in records) or not any(not key[-1] for key in records):
        raise ValueError("instrument did not observe both retention root entries")
    proof_after = _proof_hashes(proof_paths)
    if _live_sources(gate, tree, paths) != sources or proof_after != proof_before:
        raise ValueError("source or proof inputs changed during corpus analysis")
    row_errors = []
    added, removed = [], []
    for site in sorted(graph_rows.keys() | legacy_rows.keys()):
        if site in incomplete_sites:
            continue
        new, old = set(graph_rows.get(site, ())), set(legacy_rows.get(site, ()))
        additions, removals = sorted(new - old), sorted(old - new)
        if removals or (additions and site not in cyclic_sites):
            row_errors.append(f"consumer row mismatch at {site}: added={additions} removed={removals}")
        if additions:
            added.append({"site": list(site), "rows": additions})
        if removals:
            removed.append({"site": list(site), "rows": removals})
    graph_emitted = {row for rows in graph_rows.values() for row in rows}
    legacy_emitted = {row for rows in legacy_rows.values() for row in rows}
    if (set(failures) - graph_emitted) != (set(legacy_failures) - legacy_emitted):
        row_errors.append("non-consumer failure rows differ between graph and legacy passes")
    result: dict[str, Any] = {
        "tree": str(tree),
        "sources": sources,
        "units": len(units),
        "cpu_s": time.process_time() - started,
        "source_policy": "UTF-8 universal-newline text; sha256 of exact SourceUnit.source encoded as UTF-8",
        "proof": proof_before,
        "frozen": True,
        "oracle_budget": oracle_budget,
        "visits": visits,
        "records": list(records.values()),
        "failures": failures,
        "legacy_failures": legacy_failures,
        "graph_budget_trips": sorted(graph_budget_trips),
        "combination_attempts": dict(sorted(combination_attempts.items())),
        "consumer_work": dict(sorted(consumer_work.items())),
        "consumer_inventory": dict(consumer_counts[False]),
        "cyclic_consumers": len(cyclic_sites),
        "legacy_incomplete_sites": sorted(incomplete_sites),
        "rows_added": added,
        "rows_removed": removed,
    }
    result["errors"] = validate(result) + row_errors
    result["errors"].extend(row for row in failures if row.startswith("retention budget exceeded at "))
    result["classes"] = dict(Counter("acyclic" if record["state"]["acyclic"] else "cyclic" for record in records.values()))
    result["oracle_capped"] = sum(record["oracle"] is None for record in records.values())
    result["legacy_consumers_capped"] = len(incomplete_sites)
    emit("complete", errors=len(result["errors"]), oracle_capped=result["oracle_capped"], legacy_consumers_capped=len(incomplete_sites))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tree", type=Path)
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--oracle-budget", type=int, default=2_000)
    parser.add_argument("--reverse", action="store_true")
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--progress-jsonl", type=Path, help="write and flush per-pass/per-unit progress to a new private file")
    args = parser.parse_args()
    if args.oracle_budget < 1:
        parser.error("--oracle-budget must be positive")
    if args.progress_jsonl is None:
        result = measure(args.tree.resolve(), args.oracle_budget, reverse=args.reverse, paths=tuple(args.path))
    else:
        with args.progress_jsonl.open("x", encoding="utf-8") as stream:

            def report_progress(event: dict[str, Any]) -> None:
                stream.write(json.dumps(event, separators=(",", ":")) + "\n")
                stream.flush()

            result = measure(
                args.tree.resolve(), args.oracle_budget, reverse=args.reverse, paths=tuple(args.path), progress=report_progress
            )
    if args.compare is not None:
        result["errors"].extend(compare(json.loads(args.compare.read_text()), result))
    args.json.write_text(json.dumps(result, separators=(",", ":")))
    summary = {
        key: value
        for key, value in result.items()
        if key not in {"records", "sources", "failures", "legacy_failures", "legacy_incomplete_sites", "rows_added", "rows_removed"}
    }
    summary["added_consumer_sites"] = len(result.get("rows_added", ()))
    summary["removed_consumer_sites"] = len(result.get("rows_removed", ()))
    print(json.dumps(summary, indent=2))
    incomplete = result["oracle_capped"] or result.get("legacy_consumers_capped", 0)
    return 1 if result["errors"] else (2 if incomplete else 0)


if __name__ == "__main__":
    raise SystemExit(main())
