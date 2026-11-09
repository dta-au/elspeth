"""Isolated imported-object mutants: real compose/custody, unchanged source bytes."""
import __future__
import ast
import json
from pathlib import Path
import sys

import pytest

from elspeth.web.composer import tool_batch

mode = sys.argv[1]
assert mode in {"completed-denied", "applied-batch"}
original_loop = tool_batch.run_tool_batch
original_batch = tool_batch.tool_batch_progress_event

if mode == "completed-denied":
    tree = ast.parse(Path(tool_batch.__file__).read_text())
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_tool_batch")
    inserted = 0
    for node in ast.walk(function):
        if not isinstance(node, ast.If):
            continue
        for index, statement in enumerate(tuple(node.body)):
            if (isinstance(statement, ast.Raise) and isinstance(statement.exc, ast.Call)
                    and isinstance(statement.exc.func, ast.Name) and statement.exc.func.id == "_pending_proposal_cap_error"):
                false_completion = ast.parse("await emit_progress(progress, tool_completed_progress_event(tool_name, True))").body[0]
                node.body.insert(index, ast.copy_location(false_completion, statement))
                inserted += 1
    assert inserted == 2, inserted
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    exec(compile(module, tool_batch.__file__, "exec", flags=__future__.annotations.compiler_flag), tool_batch.__dict__)
else:
    def false_applied_batch(names):
        actual = original_batch(names)
        return actual.model_copy(update={
            "phase": "validating",
            "headline": "The composer has updated the pipeline and is validating the result.",
            "evidence": ("A pipeline-editing tool completed successfully.",),
        })
    tool_batch.tool_batch_progress_event = false_applied_batch

prefix = "tests/unit/web/composer/test_compose_loop_persistence.py::"
selected = [
    prefix + "test_explicit_approval_batch_caps_after_first_ten_admitted_publications",
    prefix + "test_proposal_cap_excludes_schema_errors_and_semantic_refusals",
    prefix + "test_proposal_attempt_cap_includes_approval_required_blob_only_mutation",
]

class Outcomes:
    def __init__(self):
        self.failed = []
        self.passed = []
        self.errors = []

    def pytest_runtest_logreport(self, report):
        if report.when == "call":
            if report.failed:
                self.failed.append(report.nodeid)
            elif report.passed:
                self.passed.append(report.nodeid)
        elif report.failed:
            self.errors.append(report.nodeid)

outcomes = Outcomes()
try:
    root = Path.cwd()
    code = pytest.main(["-n", "0", "-o", f"pythonpath={root / 'src'} {root / 'elspeth-lints/src'}", *selected], plugins=[outcomes])
finally:
    tool_batch.run_tool_batch = original_loop
    tool_batch.tool_batch_progress_event = original_batch
print(json.dumps({"mutant": mode, "failed": outcomes.failed, "passed": outcomes.passed, "errors": outcomes.errors}, indent=2))
assert not outcomes.errors
if mode == "completed-denied":
    expected = {
        selected[0] + "[11]",
        selected[2] + "[admitted-eleventh]",
    }
    assert set(outcomes.failed) == expected, outcomes.failed
    assert len(outcomes.passed) == 4, outcomes.passed
else:
    assert len(outcomes.failed) == 6, outcomes.failed
    assert not outcomes.passed
assert code == 1, code
raise SystemExit(int(code))
