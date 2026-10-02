# Quota enforcement repair

The reported AWS deployment enabled storage and daily token quotas. Two source
defects explain its failures: `quota_policies` used PostgreSQL's 32-bit
`INTEGER` for a configured 5 GiB allowance (5,368,709,120 bytes), and chargeable
admission deliberately refused every applicable token policy because no
production adapter populated the token ledger.

The repair is being integrated directly on `release/0.8.1`. No AWS deployment,
database recreation, or service restart is part of this local change.

## Storage limits

The three policy quantities use `BIGINT`. Configuration and administrative CLI
inputs reject quantities above the signed 64-bit limit. A PostgreSQL regression
activates the configured 5 GiB storage policy and token/control thresholds above
the former integer limit. Individual blob columns are unchanged: widening
policy limits does not require changing the per-blob size contract.

This repairs policy storage. The separate identity-wide storage enforcement
work described by the I2 plan is not implemented by this change; existing
per-session blob limits remain the implemented byte-admission control.

## Daily token accounting

Every session-owned provider attempt requires admission before dispatch.
Composer, guided work, diagnostics, signoff, auto-title, runtime source and
transform calls, retries, and completion preflights participate. Successful
and failed dispatched calls retain reported usage; absent measures remain
unknown, including after a timeout. Hidden SDK retries are disabled where
they would otherwise bypass attempt accounting.

A pending-attempt record preserves dispatch intent across interruption. A
terminal outcome settles that record and records usage using the immutable
audit call identity. Composer checkpoints persist terminal audit and usage in
one transaction; later cohorts replay that identity without charging again.
Runtime settlement follows the durable Landscape call, with idempotent
reconciliation available after interruption.

Admission compares prompt plus completion tokens with the lower applicable
identity cap and container ceiling. Active policies apply independently of
issuance defaults. A missing required policy still refuses. A known empty day
is zero; unknown terminal usage or an unresolved attempt is not zero.

The accounting day is UTC on the sessions database clock. Completed usage
belongs to the terminal audit timestamp: Composer's completion time or
Landscape's recorded call time. Pending attempts belong to their database
dispatch day. Replaying a call preserves its original day. The limit remains
eventually consistent: a call admitted below it can exceed it, and concurrent
calls already admitted may complete. Later calls check the updated total.

Pending attempts belonging to the same validated live session operation may
coexist for parallel calls. A new operation does not inherit that exemption.
Archival preserves both usage and attempts through soft archival, so it cannot
erase spend or fail simply because a session used the model.

## Schema and verification boundary

Sessions epoch 58 introduces 64-bit policy limits, nullable unknown usage, and
the pending-attempt lifecycle. Landscape epoch 42 rejects older admission
evidence payloads: version 2 records measured quota facts, so retaining the
previous epoch would accept a database whose stored evidence no longer decodes.
These are pre-release schema cutovers, not compatibility migrations. Operators
must follow the deployment's existing recreation procedure before deploying
against older stores.

Focused regressions cover daily rollover, unknown usage, per-call dispatch
refusal, replay, archival, and the 5 GiB quantity bounds; 620 backend tests
pass. Ruff, formatting, mypy, and the cross-boundary contract census pass.
The frontend session-store regression passes 116 tests, with TypeScript and
ESLint clean. PostgreSQL testcontainer execution is unavailable in this
environment because socket creation/Docker access is denied, and the default
pytest gate stops at an external Azurite fixture for the same sandbox network
restriction. These results are local evidence only, not live AWS acceptance.
