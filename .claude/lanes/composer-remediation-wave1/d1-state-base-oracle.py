"""Run final behavioral pins against the immutable base module, without tree edits."""

import ast
import subprocess
import sys
from pathlib import Path

import pytest


class BaseAdmission:
    def pytest_configure(self, config):
        import elspeth
        import elspeth_lints
        import elspeth.web.composer.tool_batch as batch

        print("elspeth:", elspeth.__file__)
        print("elspeth_lints:", elspeth_lints.__file__)
        source = subprocess.run(
            ["git", "show", "66739807c9acbdae6f2d7d9f1de858b8e69c9793:src/elspeth/web/composer/tool_batch.py"],
            check=True, capture_output=True, text=True,
        ).stdout
        self.source = source
        print("Admission module replaced in memory with exact base 66739807c9acbdae6f2d7d9f1de858b8e69c9793")
        exec(compile(source, batch.__file__, "exec"), batch.__dict__)

    def pytest_collection_modifyitems(self, items):
        for module in {item.module for item in items}:
            if module.__name__ != "tests.unit.web.composer.test_tool_result_envelope_gate":
                continue
            original_parse = module._parse

            def parse(path, original=original_parse, tool_batch=module.TOOL_BATCH, source=self.source):
                return ast.parse(source) if path == tool_batch else original(path)

            module._parse = parse
            print("Tool-batch AST reads also use immutable base; every other path remains current/unchanged")


arguments = sys.argv[1:]
if "--envelope-baseline" in arguments:
    arguments.remove("--envelope-baseline")
    lines = Path(__file__).with_name("d1-state-contract-gates.log").read_text().splitlines()
    failures = [line.split()[1] for line in lines if line.startswith("FAILED ")]
    assert len(failures) == 15
    selected = [node for node in failures if not node.endswith("::test_prevalidation_rejected_result_states_the_unapplied_envelope_on_every_field")]
    assert len(selected) == 14
    print("Exact baseline failure selection:", *selected, sep="\n")
    arguments = selected + arguments

raise SystemExit(pytest.main(arguments, plugins=[BaseAdmission()]))
