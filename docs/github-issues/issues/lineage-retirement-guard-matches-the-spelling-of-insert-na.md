---
title: Lineage retirement guard matches the spelling of insert, so aliased writers are invisible
labels: [area/tests, area/audit, type/bug]
---

The whole-tree gate that enforces a single write path into `token_lineage_frames` decides "this module writes the table" by matching the literal spelling `insert(<bare name>)`. Three spellings that write the same table produce no hit, so the gate can stay green with a second writer present.

## Why

`_modules_inserting_into` in `tests/unit/architecture/test_lineage_retirement_guard.py` has two branches. The attribute branch (`<table_or_alias>.insert()`) accepts either a `Name` or an `Attribute` receiver. The functional branch, at line 110, does not:

```python
elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "insert":
    for arg in node.args:
        if isinstance(arg, ast.Name) and (arg.id == table_attr or arg.id in aliases_to_table):
```

It requires the callable to be a bare `Name` spelled exactly `insert`, and the table operand to be a bare `Name`. The hard constraint it lacks is resolution by import. Each of the following writes to the tracked table and is invisible to the scan, leaving `test_token_lineage_frames_has_one_write_path` — which asserts the writer set equals `{"core/landscape/data_flow/tokens.py"}` — green:

- `import sqlalchemy as sa; sa.insert(token_lineage_frames_table)` — an `Attribute` callable, so the branch is never entered.
- `from sqlalchemy.dialects.postgresql import insert as postgresql_insert; postgresql_insert(token_lineage_frames_table)` — the callable is not spelled `insert`. This alias is live house style in the same subsystem: `core/landscape/execution/node_states.py`, `sink_effect_reservation.py` and `artifacts.py`, and `core/landscape/scheduler/work_items.py` and `group_losses.py` all bind `insert as postgresql_insert` and `insert as sqlite_insert`.
- `insert(models.token_lineage_frames_table)` — an `Attribute` argument, which the operand loop rejects.

The two branches are asymmetric on exactly the axis a copy-paste from a neighbouring upsert would cross.

## Impact

The gate is a soft constraint: it reads as an enforced sole-write-path rule but can be bypassed without intent, simply by following the import style already used next door. A second writer would not be reported. A sibling gate over the web writer set was found to have the same shape.

## Fix

Resolve the callable from the file's `ImportFrom` aliases rather than from its spelling — the set of names bound from an `insert` import, plus the bare `insert` — and accept `Name` or `Attribute` for the table operand, matching the attribute branch. Pin it with one synthetic module per unmatched form and assert the writer set grows; a gate that does not go red against the plants is not doing its job.

More generally, a gate that matches a name's spelling is laundered by an alias. Dispatch keyed on the spelling of an imported name is a plausible lint-rule candidate, since it generalises across both guards.

## Limits

Not measured: whether any current module already writes the table under an unmatched spelling. The claim here is the gate's blindness, demonstrated by reading the predicate. Showing a live escape would need the three synthetic plants run against the guard.
