---
title: Acceptance harness trust boundary documents a naive-datetime rejection that cannot fire
labels: [area/deployment, type/bug, priority/P3]
---

ELSPETH marks the places where it parses a value it does not own with a `@trust_boundary`
decorator whose `invariant=` text states what the function guarantees. One such boundary, in
the AWS ECS acceptance harness, declares that it rejects a datetime carrying no timezone. The
only production caller stamps UTC onto a naive value before the boundary sees it, so that
clause of the invariant is unreachable. The documentation overstates what is enforced; nothing
misbehaves.

## Where to start

`src/elspeth/web/_aws_ecs_acceptance/operator_telemetry.py`. The boundary is
`_durable_landscape_started_at` at `:484`, decorated at `:470`; the default reader is the
return at `:628`. This module is a deployment-acceptance checker — the package docstring reads
"Private implementation modules for the AWS ECS acceptance facade" — not part of the engine's
data path. No row, audit record or Landscape write depends on it. That is why this is low
priority.

## Why

The decorator's `invariant=` text says the function "raises OperatorTelemetryAcceptanceError on
a value that is not a timezone-aware datetime, or that cannot be normalised to UTC". The
function does exactly that at `:485`. But the default reader that feeds it ends with:

```python
return started_at.replace(tzinfo=UTC) if started_at.tzinfo is None else _durable_landscape_started_at(started_at)
```

so a naive run start read back from the Landscape — ELSPETH's audit database — is normalised
on the way past and never reaches the rejection. The
boundary is still called on the reader's result (`:547`, `:594`), and the clause is exercised
by the pinned test at
`tests/unit/web/aws_ecs_acceptance/test_operator_telemetry.py:1272`, but only by calling the
function directly. With an injected reader the clause can fire; with the shipped one it cannot.

## Fix

Narrow the invariant text so it describes what is enforced: the production reader
pre-normalises a naive value, and the rejection covers injected readers and values that cannot
be converted at all. Correct behaviour is that the declared invariant and the reachable
behaviour agree; you would know it holds by reading the decorator against the two call sites.

The other apparent option — route the naive value through the boundary and let it reject — is
not available, and this is worth recording so it is not tried again. The Landscape column is
declared timezone-aware (`src/elspeth/core/landscape/schema.py:504`), but SQLite has no
timezone-aware type and returns a naive value on read, measured with a control that a plain
assignment preserves the timezone. The harness reaches SQLite by default:
`resolve_landscape_url` (`src/elspeth/web/config.py:1402-1411`) falls back to a file under the
data directory whenever no Landscape URL is configured. Routing the value through the boundary
would therefore reject nearly every run start on a default-configured deployment. On a
PostgreSQL-backed deployment the read comes back timezone-aware and the branch is unreachable
for the opposite reason. Either way the change belongs in the text.

**Size.** A few lines of prose in a decorator. One caveat: the `invariant=` text is covered by
the repository's static-analysis allowlist, so changing the wording also requires that
allowlist entry to be refreshed — worth sequencing alongside other work on that gate rather
than doing on its own.
