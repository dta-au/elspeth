"""Positive and negative controls for retention corpus completeness and deltas."""

from __future__ import annotations

import ast
import copy
import json
import sys

import pytest
from scripts import fencing_retention_corpus as corpus

from tests.unit.architecture import test_web_landscape_mutation_fencing as fencing


def _control():
    return {
        "sources": {"probe.py": "source-digest"},
        "failures": ["retained enum producer return is unclosed probe.py:3"],
        "records": [
            {
                "key": ["probe.py", 1, False, False],
                "state": {"origins": [None], "retained": False, "acyclic": True},
                "oracle": {"origin": None, "retained": False},
            },
            {
                "key": ["probe.py", 2, False, True],
                "state": {"origins": [None], "retained": True, "acyclic": False},
                "oracle": {"origin": None, "retained": True},
            },
        ],
    }


def test_comparator_accepts_identical_complete_control():
    control = _control()
    assert corpus.validate(control) == []
    assert corpus.compare(control, copy.deepcopy(control)) == []


@pytest.mark.parametrize("side", ("left", "right"))
def test_comparator_rejects_duplicate_root_keys(side):
    left, right = _control(), _control()
    duplicate = left if side == "left" else right
    duplicate["records"].append(copy.deepcopy(duplicate["records"][0]))
    with pytest.raises(ValueError, match="duplicate retention root key"):
        corpus.compare(left, right)


def test_comparator_rejects_missing_roots_and_changed_source_inventory():
    left, right = _control(), _control()
    right["records"].pop()
    assert any("root inventory differs" in error for error in corpus.compare(left, right))
    right = _control()
    right["sources"]["probe.py"] = "changed-source"
    assert "source digest inventory differs" in corpus.compare(left, right)


@pytest.mark.parametrize("field,changed", (("origins", [["enum-value", "probe.Enum"]]), ("retained", True)))
def test_comparator_rejects_acyclic_origin_and_retained_mismatches(field, changed):
    left, right = _control(), _control()
    right["records"][0]["state"][field] = changed
    errors = corpus.compare(left, right)
    assert any("acyclic R/O mismatch" in error for error in errors)
    assert any("graph state differs" in error for error in errors)


def test_comparator_rejects_dropped_cyclic_origin_retention_and_failure_row():
    left, right = _control(), _control()
    right["records"][1]["state"] = {"origins": [["enum-value", "probe.Enum"]], "retained": False, "acyclic": False}
    right["failures"] = []
    errors = corpus.compare(left, right)
    assert any("cyclic R/O inclusion failure" in error for error in errors)
    assert "failure row sets differ" in errors


def test_capped_oracle_is_uncompared_and_never_classified_by_finishing():
    control = _control()
    control["records"][0]["oracle"] = None
    assert corpus.validate(control) == []
    assert control["records"][0]["state"]["acyclic"] is True
    control["records"][1]["oracle"] = None
    assert control["records"][1]["state"]["acyclic"] is False


def test_oracle_budget_stops_before_publishing_unbalanced_frame():
    frames = corpus._OracleFrames(1)
    first = object()
    frames.append(first)
    with pytest.raises(corpus._OracleBudgetExceeded):
        frames.append(object())
    assert frames == [first]
    assert frames.evaluations == 2


@pytest.mark.parametrize("capped,errors,expected", ((0, [], 0), (1, [], 2), (0, ["deliberate mismatch"], 1)))
def test_main_distinguishes_complete_partial_and_mismatched_results(monkeypatch, tmp_path, capped, errors, expected):
    result = _control() | {"errors": errors, "oracle_capped": capped}
    monkeypatch.setattr(corpus, "measure", lambda *_args, **_kwargs: copy.deepcopy(result))
    output = tmp_path / "measured.json"
    monkeypatch.setattr(sys, "argv", ["fencing_retention_corpus", str(tmp_path), "--json", str(output)])
    assert corpus.main() == expected
    assert json.loads(output.read_text())["oracle_capped"] == capped


def test_main_fails_on_deliberate_comparison_mismatch(monkeypatch, tmp_path):
    previous = tmp_path / "previous.json"
    previous.write_text(json.dumps(_control()))
    changed = _control() | {"errors": [], "oracle_capped": 0}
    changed["failures"] = []
    monkeypatch.setattr(corpus, "measure", lambda *_args, **_kwargs: changed)
    output = tmp_path / "measured.json"
    monkeypatch.setattr(sys, "argv", ["fencing_retention_corpus", str(tmp_path), "--json", str(output), "--compare", str(previous)])
    assert corpus.main() == 1
    assert "failure row sets differ" in json.loads(output.read_text())["errors"]


@pytest.fixture
def measured_source(monkeypatch, tmp_path):
    relative = "src/elspeth/measured_probe.py"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    source = "def held(value):\n    safe = value\n    return safe\n"
    path.write_text(source, encoding="utf-8")
    unit = fencing._parse_source(relative, source)
    proof_aliases = {
        "tests/unit/architecture/test_web_landscape_mutation_fencing.py": fencing.__file__,
        "tests/helpers/web_reserve_retention.py": fencing._reserve_retention.__file__,
    }
    for relative_proof, actual in proof_aliases.items():
        alias = tmp_path / relative_proof
        alias.parent.mkdir(parents=True, exist_ok=True)
        alias.symlink_to(actual)
    files = [path]
    monkeypatch.setattr(corpus, "load_gate", lambda _tree: fencing)
    monkeypatch.setattr(fencing, "iter_gate_files", lambda _root: iter(files))
    monkeypatch.setattr(fencing, "_production_units", lambda: (unit,))
    return tmp_path, path, source, unit, files


def test_real_measure_observes_both_roots_and_legacy_rows_on_frozen_source(measured_source):
    root, _path, _source, unit, _files = measured_source
    result = corpus.measure(root, 200)
    assert result["units"] == 1 and result["frozen"] is True
    assert result["errors"] == [] and result["oracle_capped"] == result["legacy_consumers_capped"] == 0
    assert {record["key"][-1] for record in result["records"]} == {False, True}
    assert result["consumer_inventory"] == {unit.path: len(tuple(ast.walk(unit.tree)))}
    assert result["rows_added"] == result["rows_removed"] == []
    assert result["failures"] == result["legacy_failures"]


@pytest.mark.parametrize("failure", ("progress", "analysis"))
def test_real_measure_restores_observers_when_progress_or_analysis_fails(monkeypatch, measured_source, failure):
    root, _path, _source, _unit, _files = measured_source
    original_cache = fencing.cache
    original_combinations = fencing._reserve_retention.consumer_combinations
    original_analysis = fencing._reserve_selected_origin_effect_failures
    analysis_error = RuntimeError("controlled analysis failure")
    progress_error = OSError("controlled progress sink failure")
    finished = []

    def failing_progress(event):
        if event["event"] == "unit_finished":
            finished.append(event)
            raise progress_error

    def failing_analysis(units):
        original_analysis(units)
        raise analysis_error

    with monkeypatch.context() as patch:
        if failure == "analysis":
            patch.setattr(fencing, "_reserve_selected_origin_effect_failures", failing_analysis)
        expected = analysis_error if failure == "analysis" else progress_error
        with pytest.raises(type(expected)) as caught:
            corpus.measure(root, 200, progress=failing_progress)
        assert caught.value is expected
        assert fencing.cache is original_cache
        assert fencing._reserve_retention.consumer_combinations is original_combinations
        assert len(finished) == (0 if failure == "analysis" else 1)

    assert corpus.measure(root, 200)["errors"] == []


def test_real_measure_and_cli_reject_empty_production_authority(monkeypatch, measured_source):
    root, path, _source, _unit, _files = measured_source
    monkeypatch.setattr(fencing, "_production_units", lambda: ())
    with pytest.raises(ValueError, match="empty production source inventory"):
        corpus.measure(root, 200)
    monkeypatch.setattr(sys, "argv", ["fencing_retention_corpus", str(root), "--json", str(path.with_suffix(".json"))])
    with pytest.raises(ValueError, match="empty production source inventory"):
        corpus.main()


def test_real_measure_rejects_parsed_source_and_live_file_mismatch(measured_source):
    root, path, _source, _unit, _files = measured_source
    path.write_text("def held(value):\n    return [value]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="parsed source differs"):
        corpus.measure(root, 200)


def test_real_measure_rejects_source_change_between_inventory_and_parse(monkeypatch, measured_source):
    root, path, _source, unit, _files = measured_source

    def change_then_return():
        path.write_text("def held(value):\n    return [value]\n", encoding="utf-8")
        return (unit,)

    monkeypatch.setattr(fencing, "_production_units", change_then_return)
    with pytest.raises(ValueError, match="parsed source differs"):
        corpus.measure(root, 200)


@pytest.mark.parametrize("change_inventory", (False, True))
def test_real_measure_rejects_content_or_inventory_drift_during_analysis(monkeypatch, measured_source, change_inventory):
    root, path, _source, _unit, files = measured_source
    original = fencing._reserve_selected_origin_effect_failures

    def change_after_analysis(units):
        result = original(units)
        if change_inventory:
            if len(files) == 1:
                extra = path.with_name("arrived_probe.py")
                extra.write_text("value = 0\n", encoding="utf-8")
                files.append(extra)
        else:
            path.write_text("def held(value):\n    return [value]\n", encoding="utf-8")
        return result

    monkeypatch.setattr(fencing, "_reserve_selected_origin_effect_failures", change_after_analysis)
    with pytest.raises(ValueError, match="inputs changed during corpus analysis"):
        corpus.measure(root, 200)


def test_real_measure_binds_the_declared_universal_newline_source_policy(measured_source):
    root, path, source, _unit, _files = measured_source
    path.write_bytes(source.replace("\n", "\r\n").encode("utf-8"))
    result = corpus.measure(root, 200)
    assert result["frozen"] is True and result["errors"] == []
    assert "universal-newline" in result["source_policy"]


def test_real_measure_rejects_proof_change_across_import_boundary(monkeypatch, measured_source):
    root, _path, _source, _unit, _files = measured_source
    original_loader = corpus.load_gate
    loaded = []

    def change_during_import(tree):
        graph_alias = tree / "tests/helpers/web_reserve_retention.py"
        graph_alias.unlink()
        graph_alias.write_text("# controlled import-boundary replacement\n", encoding="utf-8")
        loaded.append(True)
        return original_loader(tree)

    monkeypatch.setattr(corpus, "load_gate", change_during_import)
    with pytest.raises(ValueError, match="proof inputs changed across gate import"):
        corpus.measure(root, 200)
    assert loaded == [True]


@pytest.mark.parametrize("capture", (False, True))
def test_real_legacy_root_caps_mark_consumers_incomplete_and_cli_partial(monkeypatch, measured_source, capture):
    root, path, _source, previous, _files = measured_source
    source = "def held(default=opaque(0)):\n    return None\n" if capture else "def held(value):\n    return (value, value)\n"
    path.write_text(source, encoding="utf-8")
    unit = fencing._parse_source(previous.path, source)
    monkeypatch.setattr(fencing, "_production_units", lambda: (unit,))
    result = corpus.measure(root, 1)
    assert result["errors"] == []
    assert any(record["key"][-1] is capture and record["oracle"] is None for record in result["records"])
    assert result["legacy_consumers_capped"] > 0 and result["legacy_incomplete_sites"]
    output, progress = root / "capped.json", root / "capped-progress.jsonl"
    monkeypatch.setattr(
        sys,
        "argv",
        ["fencing_retention_corpus", str(root), "--json", str(output), "--oracle-budget", "1", "--progress-jsonl", str(progress)],
    )
    assert corpus.main() == 2
    persisted = json.loads(output.read_text())
    events = [json.loads(line) for line in progress.read_text().splitlines()]
    assert persisted["legacy_consumers_capped"] > 0
    assert {event["legacy"] for event in events if event["event"] == "unit_started"} == {False, True}
    assert [event["event"] for event in events][-1] == "complete"


@pytest.mark.parametrize("duplicate", ("live", "parsed"))
def test_real_measure_rejects_duplicate_source_authorities(monkeypatch, measured_source, duplicate):
    root, path, _source, unit, files = measured_source
    if duplicate == "live":
        files.append(path)
    else:
        monkeypatch.setattr(fencing, "_production_units", lambda: (unit, unit))
    with pytest.raises(ValueError, match=r"duplicate .* source path"):
        corpus.measure(root, 200)


@pytest.mark.parametrize("mutation", ("missing", "duplicate", "reordered"))
def test_real_measure_rejects_missing_duplicate_or_reordered_consumer_invocations(monkeypatch, measured_source, mutation):
    root, _path, _source, unit, _files = measured_source
    original_analysis = fencing._reserve_selected_origin_effect_failures
    changed = []

    def altered_analysis(units):
        observed = fencing._reserve_retention.consumer_combinations

        def alter(consumer, expressions, charge):
            if consumer.__defaults__[0] is unit.tree:
                changed.append(True)
                if mutation == "missing":
                    return ()
                if mutation == "duplicate":
                    observed(consumer, expressions, charge)
                else:
                    consumer.__defaults__ = (unit.tree.body[0],)
            return observed(consumer, expressions, charge)

        fencing._reserve_retention.consumer_combinations = alter
        try:
            return original_analysis(units)
        finally:
            fencing._reserve_retention.consumer_combinations = observed

    monkeypatch.setattr(fencing, "_reserve_selected_origin_effect_failures", altered_analysis)
    with pytest.raises(ValueError, match="missing, duplicate or reordered consumer"):
        corpus.measure(root, 200)
    assert changed == [True]


def test_real_measure_detects_dropped_cyclic_legacy_row_with_unchanged_root_values(monkeypatch, measured_source):
    root, path, _source, previous, files = measured_source
    supplier_relative = "src/elspeth/contracts/prompt_guard.py"
    supplier_path = root / supplier_relative
    supplier_path.parent.mkdir(parents=True)
    supplier_source = "from enum import Enum\nclass PromptGuardTrust(Enum):\n    REFERENCE = 'reference'\n"
    supplier_path.write_text(supplier_source, encoding="utf-8")
    supplier = fencing._parse_source(supplier_relative, supplier_source)
    source = (
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n"
        "    cycle = cycle\n    value = getattr(cycle, '__annotations__', PromptGuardTrust.REFERENCE)\n"
        "    value = vars(PromptGuardTrust.REFERENCE)\n    return value\n"
    )
    path.write_text(source, encoding="utf-8")
    unit = fencing._parse_source(previous.path, source)
    returned = unit.tree.body[1].body[-1].value
    files.append(supplier_path)
    monkeypatch.setattr(fencing, "_production_units", lambda: (supplier, unit))
    original_analysis = fencing._reserve_selected_origin_effect_failures
    original_combinations = fencing._reserve_retention.consumer_combinations
    phase = {"calls": 0}

    def analysis(units):
        phase["calls"] += 1
        return original_analysis(units, additional_supplier_roots=frozenset({"elspeth.contracts.prompt_guard.PromptGuardTrust"}))

    def mutated_projection(consumer, expressions, charge):
        def projected(value, use, *, read_target=False):
            original = expressions(value, use, read_target=read_target)
            if phase["calls"] == 1 and value is returned:
                return frozenset(origin for origin in original if origin is None or origin[0] != "enum-compound")
            return original

        return original_combinations(consumer, projected, charge)

    monkeypatch.setattr(fencing, "_reserve_selected_origin_effect_failures", analysis)
    monkeypatch.setattr(fencing._reserve_retention, "consumer_combinations", mutated_projection)
    result = corpus.measure(root, 500)
    assert corpus.validate(result) == []
    assert result["legacy_consumers_capped"] == 0
    assert any("consumer row mismatch" in error for error in result["errors"])
    assert any("retained enum producer return is unclosed " in row for site in result["rows_removed"] for row in site["rows"])


def test_real_measure_detects_consumer_row_drift_with_unchanged_root_values(monkeypatch, measured_source):
    root, _path, _source, unit, _files = measured_source
    returned = unit.tree.body[0].body[-1].value
    original_analysis = fencing._reserve_selected_origin_effect_failures
    original_combinations = fencing._reserve_retention.consumer_combinations
    phase = {"calls": 0}

    def analysis(units):
        phase["calls"] += 1
        return original_analysis(units)

    def mutated_projection(consumer, expressions, charge):
        def projected(value, use, *, read_target=False):
            original = expressions(value, use, read_target=read_target)
            if phase["calls"] == 1 and value is returned:
                return frozenset({("enum-value", "probe.Enum")})
            return original

        return original_combinations(consumer, projected, charge)

    monkeypatch.setattr(fencing, "_reserve_selected_origin_effect_failures", analysis)
    monkeypatch.setattr(fencing._reserve_retention, "consumer_combinations", mutated_projection)
    result = corpus.measure(root, 200)
    assert corpus.validate(result) == [], "root R/O comparison alone cannot detect this projector mutation"
    assert any("consumer row mismatch" in error for error in result["errors"])
    assert result["rows_added"] and not result["rows_removed"]
