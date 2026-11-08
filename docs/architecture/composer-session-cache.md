# Composer session analysis reuse

The web application's `ComposerSessionCacheRegistry` owns bounded, process-local
memo storage for authenticated persisted Composer sessions. A borrow is scoped to
both principal and session. Ownership, current head, hydration, authorization,
policy checks, runtime preflight, and provider/advisor decisions remain live.
An equal reconstructed draft can reuse analysis; a different draft with the same
version cannot. Process restarts start cold and require no database migration.

## Configuration and limits

`ELSPETH_WEB__COMPOSER_SESSION_CACHE_ENABLED=false` disables retained analysis and
session discovery/projection reuse. It preserves live checks, provider transitions,
and bounded schema-disclosure guidance. Public immutable catalog preparation has
application lifetime and remains available when session retention is disabled.
No tutorial-specific backend or synthesized provider output is introduced.

`SessionCacheLimits` defines internal limits, rather than additional environment
settings: 128 owners, 900 seconds idle expiry, 16 validation entries, 128 discovery
entries, 32 projections, 16 pending entries per namespace, 128 queued jobs per
registry, 128 schema identities per owner, and 128 archive reservations. Retained
accounting weight is bounded by 2 MiB per owner, 64 MiB per registry, and 1 MiB per
entry. These weights include reservations and metadata; they are not an RSS bound.
Pressure falls back to ordinary computation and never waits for an idle owner.

## Eligibility and fresh decisions

Raw analysis keys include authored content and the actual plugin manager/class
registrations, including missing registrations. Unknown or replaced classes bypass
retention. Power Automate sources/sinks, database sinks, and LLM transforms using
Langfuse tracing are excluded because their constructor dependencies are not fully
versioned. Private lowered state is never retained or hashed. Profile validation
uses authored identity plus current policy snapshot and profile generation;
authorization and profile lowering still run on each use.

Catalog summaries are serialized immutable public data, prepared once per actual
registration revision, with isolated mutable response copies. Session projections
also bind policy/profile/catalog revisions. The bounded authoring-aids memo stores
public application compilation with the full revision, never private session data.

Discovery declarations explicitly select operation or session scope. Legacy
`cacheable=True` retains operation scope; `list_models` remains operation-only.
The owning argument parser runs before canonical key construction. Missing
parsers, missing revisions and unsupported keys bypass session retention. Only
successful admitted canonical payloads are retained. Every use still creates a
fresh invocation envelope, applies current projection/budget/admission, and records
a distinct audit invocation. Logical repeats are operation-local regardless of
whether the physical lookup hits an earlier operation's cache.

Schema disclosure is bounded guidance owned by the session and current complete
policy/profile/catalog revision. It records successful provider delivery, not just
a lookup. Outer-loop pending schema identities are bounded to 128 entries/64 KiB;
failed or cancelled delivery does not mark them. Revision changes conservatively
skip old marks. A reply-only review path that quotes tool evidence without the
ordinary delivery-confirmation helper can leave guidance empty; the durable transcript remains
the source of truth. Private/uncached callers use bounded operation-local guidance.

## Lifetime and concurrency

The short registry lock protects metadata, never expensive computation. Identical
work joins a single producer. Request borrows and actual worker pins have separate
lifetimes: cancelling a requester does not dispose an executing worker's owner.
Retirement reserves a session across the entire archive attempt, including cold,
overlapping, refused, failed and cancelled attempts. A retired owner cannot be
replaced until borrows and actual workers drain. Library forks get distinct owners.
Application shutdown stops admission before draining actual workers and cache jobs.

## Operational evidence

OpenTelemetry exports `composer.cache.lookups` (fixed `kind` and `result` labels),
`composer.cache.evictions` (fixed `kind`), and `composer.cache.weight` (bytes, no
labels). Kinds are validation/discovery/projection, plus owner for failed borrows;
results are hit/miss/join/bypass. Entry-removal counts include retirement and
shutdown, not only pressure eviction. Counters are process-lifetime totals;
weight sums currently living registries, including private operation fallbacks.
Weak observation does not extend registry lifetime. SDK collection happens outside
cache locks. No principal, session, graph hash, tool arguments or secret is exported.
These pressure measurements are operational telemetry, not an audit substitute.

Normally selected tests assert full result equality and deterministic compute
counts, including ten reconstructed reads and real mixed planner/outer/completion
consumers. The explicit performance suite compares disabled, cold and warm local
analysis for a shipped pipeline, an 80-node graph, an invalid draft and a profile
graph. It reports validator/probe counts, cache snapshots and elapsed time without
a hard millisecond threshold. Hydration, database, provider, guard and advisor
latency are outside this measurement; it is not an end-to-end speedup claim.

For final verification follow the frozen-tree gate and explicit performance command
in [the implementation plan](composer-session-cache-implementation-plan.md).
