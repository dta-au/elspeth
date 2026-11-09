# Task 3 / Q25 — exact expression operator documentation parity

Status: DONE — implementation and bounded verification complete, uncommitted.
Full module: 109 passed, exit 0. Worktree:
`/workspace/elspeth/.claude/worktrees/composer-remediation-discovery`, HEAD
`6825994b482bc274df2ae85d8738b9078f0117b4`.

## Changed file and behavior

Only task-owned file: `tests/unit/web/composer/test_tools_discovery.py`.
The review artifact `task-3-review.diff` contains only that file (38 additions,
2 deletions). Sibling Task 2 edits were present in the worktree and were not
edited, reverted, or included in this diff. In particular, `generation.py` was
never mutated on disk for any Task 3 control.

The real operator parity test now isolates the Operators section before
Literals, separates each operator list from its description at the two-or-more
space column separator, and comma-splits entries into an exact-membership set.
Single spaces inside `is not` and `not in` remain part of those tokens. The
ternary expression example is explicitly excluded because it is syntax, not an
operator-map entry. AST-token-table completeness still runs before text checks;
safe-constant assertions are unchanged.

Six enduring parameterized mutation cases call the real parity test with an
in-memory monkeypatched grammar return. Each removes a literal, uniquely
occurring documented entry and requires the real assertion to fail with the
specific omitted-token message. The mutation setup does not use the new
tokenizer or recreate its logic. It preserves longer operators and description
prose so they cannot mask the omitted entry. Controls cover `<`, `>`, `/`, `==`,
`is not`, and `not in`.

The original gate missed deletions of `<`, `>`, and `/` as expected. It also
missed deletion of `is not`, because the phrase remained in the Identity
description example. Original `==` and `not in` deletion controls already
failed the parity assertion correctly. This is captured in the red run:
**4 failed, 2 passed**, with all four failures being `DID NOT RAISE` from the
actual unchanged parity assertion. After the oracle repair all six mutated
grammars fail that real assertion for their specific omitted token, while
the intact grammar passes.

## Verification commands and evidence

All Python test commands ran from the task worktree with BOTH roots:

```bash
cd /workspace/elspeth/.claude/worktrees/composer-remediation-discovery
export PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"
```

Import provenance check, exit 0:

```bash
PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" .venv/bin/python -c 'import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)'
```

Actual output:

```text
/workspace/elspeth/.claude/worktrees/composer-remediation-discovery/src/elspeth/__init__.py
/workspace/elspeth/.claude/worktrees/composer-remediation-discovery/elspeth-lints/src/elspeth_lints/__init__.py
```

The common pytest command prefix was:

```bash
.venv/bin/python -m pytest -n 0 -o pythonpath="$PWD/src $PWD/elspeth-lints/src"
```

| Run | Selection after common pytest prefix | Exit/result | Lane evidence |
| --- | --- | --- | --- |
| Baseline | `tests/unit/web/composer/test_tools_discovery.py::TestDiscoveryTools::test_expression_grammar_documents_every_operator` | 0; 1 passed | `task-3-baseline.log`, `task-3-baseline.exit` |
| Red controls, before oracle repair | `tests/unit/web/composer/test_tools_discovery.py::TestDiscoveryTools::test_operator_parity_rejects_missing_standalone_token` | 1; 4 failed, 2 passed | `task-3-red.log`, `task-3-red.exit` |
| Green controls and intact/sibling grammar tests | the four node IDs below | 0; 9 passed | `task-3-green.log`, `task-3-green.exit` |
| Full assigned module | `tests/unit/web/composer/test_tools_discovery.py` | 0; 109 passed in 26.74s | `task-3-module.log`, `task-3-module.exit` |

Green selection:

```text
tests/unit/web/composer/test_tools_discovery.py::TestDiscoveryTools::test_expression_grammar_documents_every_operator
tests/unit/web/composer/test_tools_discovery.py::TestDiscoveryTools::test_operator_parity_rejects_missing_standalone_token
tests/unit/web/composer/test_tools_discovery.py::TestDiscoveryTools::test_get_expression_grammar_is_static
tests/unit/web/composer/test_tools_discovery.py::TestDiscoveryTools::test_expression_grammar_documents_every_builtin_bidirectionally
```

Ruff commands (terminal output), both exit 0:

```bash
.venv/bin/python -m ruff check tests/unit/web/composer/test_tools_discovery.py
.venv/bin/python -m ruff format --check tests/unit/web/composer/test_tools_discovery.py
```

Output: `All checks passed!`; `1 file already formatted`.

`git diff --check -- tests/unit/web/composer/test_tools_discovery.py` exited 0.
Review diff was generated with:

```bash
git diff -- tests/unit/web/composer/test_tools_discovery.py > /workspace/elspeth/.claude/lanes/composer-remediation-wave1/task-3-review.diff
```

## Limits

This is a test-only repair. No production grammar, parser, runtime behavior,
plugin contract, schema, fixture snapshot, or suppression was changed. Existing
AST completeness and safe-constant checks were retained. No new dynamic
attribute probes or mocks were introduced; the static grammar return is patched
only in-memory and restored by pytest.

Parent owns independent review, applying this one-file patch to the combined
tree, whole-tree gates and broad verification. No broad suite or PostgreSQL
suite was launched by this task. Changes remain uncommitted.
