---
title: Scope map is last-write-wins on a duplicate closer name — unreachable, defence in depth only
labels: [area/engine, type/task, priority/P4]
---

A dictionary the graph builder constructs from scope declarations silently keeps the last entry
when two of them share a closer name. No configuration can reach that state, because settings
validation rejects the duplicate before the builder runs. This is a defence-in-depth note, not
a live defect.

## Where to start

`src/elspeth/core/dag/builder.py:692`, inside `build_execution_graph` (`:181`):

```python
scopes_by_closer: dict[str, ScopeSettings] = {s.closer: s for s in (scope_settings or ())}
```

The map is read at `:707` and `:714` to find the scope that binds a collector.

## Why it is unreachable

Every producer of `scope_settings` in the shipped source takes `.scopes` off an
`ElspethSettings` object — the command-line entry points in `src/elspeth/cli.py` and the web
preflight at `src/elspeth/web/execution/preflight.py:672`. `ElspethSettings` validates scope
bindings in `_validate_scope_bindings` (`src/elspeth/core/config.py:2233-2261`), which is a
`@model_validator(mode="after")` and therefore runs on every construction of the object,
raising `Collector '<name>' is already bound — one scope per closer`.

Checked with both controls, because a validator that rejects for some other reason would look
identical: settings with two distinct closers are accepted, and settings with a shared closer
are rejected specifically on the already-bound message, not on a co-occurring error.

A test pinning that behaviour already exists —
`tests/unit/core/test_config_collectors_scopes.py:127`,
`test_two_scopes_cannot_share_a_closer`.

## Impact

None today. There is no configuration path that produces a duplicate closer, so nothing is
mis-wired and nothing is exploitable. The gap matters only if a future caller constructs
`scope_settings` without going through `ElspethSettings`, at which point one of two scope
declarations would be dropped without a word.

## Fix

The substantive gap is that the builder does not say, at the line, which upstream check it is
relying on. Either:

- add a comment at `:692` naming `_validate_scope_bindings` and the test that pins it, so the
  reliance is documented where someone would look; or
- raise explicitly at map construction if the project would rather the guard be local, in which
  case the error message should not duplicate the settings one.

You would know it holds when a reader at `:692` can tell why the duplicate case is unhandled
and what guarantees it stays that way.

**Size.** One line for the first option. Closing this without a code change is a defensible
outcome — the only argument for doing it is local readability.
