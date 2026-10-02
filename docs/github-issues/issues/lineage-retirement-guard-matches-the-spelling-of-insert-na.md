---
title: Lineage retirement guard matches the spelling of insert, so aliased writers are invisible
labels: [area/tests, area/audit, type/bug]
---

A repository-wide check enforces that exactly one module writes to the `token_lineage_frames` audit table. It decides which modules write to it by matching the literal spelling `insert(<bare name>)`, so three spellings that write the same table produce no hit and the check stays green with a second writer present.

## Where this lives

Everything is in one 127-line file, `tests/unit/architecture/test_lineage_retirement_guard.py`. It is a test that parses every module under `src/elspeth/` as a syntax tree rather than running anything, so no knowledge of the runtime is needed to work on it. The table it guards is written by `src/elspeth/core/landscape/data_flow/tokens.py`.

## Why

`_modules_inserting_into` has two branches. The attribute branch, for `<table>.insert()`, accepts either a plain name or a dotted attribute as the receiver. The functional branch, at line 110, does not:

```python
elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "insert":
    for arg in node.args:
        if isinstance(arg, ast.Name) and (arg.id == table_attr or arg.id in aliases_to_table):
```

It requires the callable to be a plain name spelled exactly `insert`, and the table operand to be a plain name too. What it lacks is resolution by import: it never looks at what the file actually imported. Each of the following writes the tracked table and is invisible, leaving `test_token_lineage_frames_has_one_write_path` — which asserts the writer set equals `{"core/landscape/data_flow/tokens.py"}` — green:

- `import sqlalchemy as sa; sa.insert(token_lineage_frames_table)`. A dotted callable, so the branch is never entered.
- `from sqlalchemy.dialects.postgresql import insert as postgresql_insert; postgresql_insert(...)`. The callable is not spelled `insert`. This alias is the existing house style in the same subsystem: `core/landscape/execution/node_states.py`, `sink_effect_reservation.py` and `artifacts.py`, and `core/landscape/scheduler/work_items.py` and `group_losses.py` all bind `insert as postgresql_insert` and `insert as sqlite_insert`.
- `insert(models.token_lineage_frames_table)`. A dotted argument, which the operand loop rejects.

The two branches are asymmetric on exactly the axis a copy-paste from a neighbouring file would cross.

## Impact

The check reads as an enforced single-writer rule but can be bypassed without anyone intending to, simply by following the import style already used next door. A second writer to an audit table would not be reported. A sibling check over the web writer set was found to have the same shape.

## Fix

Two widenings, both in `_modules_inserting_into`:

1. Resolve the callable from the file's imports instead of its spelling. Collect the names bound from an `insert` import — each `ImportFrom` alias whose original name is `insert`, contributing `alias.asname or "insert"` — union the bare `insert`, and test membership of that set rather than equality with a literal.
2. Accept a plain name or a dotted attribute for the table operand, matching what the attribute branch already accepts for its receiver.

You know it holds by proving the check can fail. Add one synthetic module per unmatched form above, run the scan over them, and assert the writer set grows to include each. A check that does not go red against those plants is not doing its job, and that negative control is the deliverable as much as the widening is.

## Size

Small: a single function in a single test file, roughly ten lines changed plus fixtures. No design decision outstanding. The one judgement call is where to put the synthetic modules so the scan sees them without shipping them in `src/`. A temporary directory is the simplest answer: `_modules_inserting_into` reads its files through `iter_gate_sources(root)`, but passes the module-level `SRC_ROOT` constant, so the root needs to become a parameter first.

There is a broader observation worth recording but not worth blocking on: a check that matches a name's spelling is defeated by an alias, and "dispatch keyed on the spelling of an imported name" would generalise to both this check and its sibling as a lint rule.

## Limits

Not measured: whether any current module already writes the table under an unmatched spelling. The claim is the check's blindness, demonstrated by reading the predicate. Showing a live escape would need the three synthetic plants run against it.
