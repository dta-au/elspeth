# Replay and verify runtime contract

Status: implementation contract for K056 (2026-09-24). The operator selected
**wire** for both modes. This note defines what a mode-aware run must prove;
acceptance remains subject to executable evidence.

## Invocation and audit authority

`run_mode` is an invocation setting. `live` retains current behavior. `replay`
uses the recorded source rows and external responses of `replay_from`, executes
the pipeline's pure computation, and publishes no configured sink effect.
`verify` reads current source data and makes the configured external calls,
compares their complete semantic results with `replay_from`, and publishes no
configured sink effect. Both modes may write their **new** Landscape audit run
and retained audit payloads; those writes are required to explain the result.
They may not silently write to a pipeline sink, export sink, tracer, probe, or
telemetry destination. An explicitly isolated side effect would require a
separate, auditable setting; no such setting is implied by `verify`.

The source run is opened read-only before any plugin lifecycle hook or
external effect. It must exist in the selected Landscape and have status
`completed`, `completed_with_failures`, or `empty`. A run with failures is
admitted only when every source was exhausted and its rows, quarantine
decisions, and required call evidence are complete. Interrupted runs are
refused. A missing or purged payload, invalid hash,
ambiguous call, or changed graph/plugin implementation is a refusal, never a
reason to fall through to a live call. The mode and source run ID are recorded
on the new run; every replayed or compared call must have queryable lineage
to the source call. `settings_json` alone records intent and does not prove
that the runtime honored it.

Compatibility is checked against the source graph, plugin implementation
identity, canonical version, source field contract, and execution-affecting
settings. Invocation-only fields (`run_mode` and `replay_from`) are excluded
from config equivalence. A plugin that cannot declare and satisfy an exact
mode contract is refused before construction or `on_start`; unknown third
party plugins are refused. This is a capability refusal, not a quiet downgrade
to `live`.

## External calls

Each recorded call is bound to the source run, source node or operation,
call type, canonical request hash, and occurrence within that parent. Source
request and response payloads needed by transforms are checked before plugin
lifecycle starts. Repeated source and runtime-preflight operations carry a
durable occurrence index; old records with indistinguishable parents fail
closed. The
global request-hash sequence in the current `CallReplayer` is insufficient:
two nodes may issue identical requests, and timestamp order alone does not
identify the intended response. Replay consumes each matched occurrence once,
reconstructs the same runtime return type from retained response data, and
records a new audit call with the source-call relationship. It refuses an
original error unless the adapter can reconstruct the same classified error.
Unmatched current calls and unconsumed source calls are run failures.

Verify makes its live call only after explicit mode admission, records the
actual result, compares it with the corresponding source call, and durably
records match, mismatch, missing-recording, or unverifiable evidence. A
mismatch, missing recording, missing payload, or unconsumed source call makes
the verify run fail and produces a nonzero CLI result. In-memory
`VerificationReport` counters are useful diagnostics but are not the verdict.
Comparisons are exact by default. Narrow adapter-specific identities, such as
Azure managed-identity Authorization rotation, compare all execution-relevant
non-authentication fields and record the current credential fingerprint
separately; they never claim that a source-run credential was reacquired.
There is no general ignored-path setting.

## Sources, sinks, and preflight

Replay reconstructs source rows, source identity, field resolution, row order,
validation/quarantine outcomes, and payload hashes from the source run. It
does not call the configured source's `on_start`, `load`, or cleanup hooks.
Verify may use the live source only after admission and compares the source
row stream and contract against the source run before transform processing.
The source comparison includes empty and quarantined streams.

Replay reads only retained payloads named by the source-run audit. A computed
transform output may be stored in the new audit run only when its hash is an
explicitly audited source-run output reference and its retained bytes match.
Future live PDF runs record renderer identity and a typed render receipt so
replay can restore pages without launching a worker; legacy PDF runs without
that evidence are refused. Verify pre-admits a source's retained call before
credential acquisition, SDK construction, DNS, or transport where the source
has such effects, then records a durable comparison for the actual call.

Both modes compute proposed sink effects and compare their sink-boundary node,
member identities, dispositions, and canonical member payload hashes with the
source run. This proves parity at the sink boundary; it does not claim parity
of bytes that a sink would serialize or publish. They do not call sink `on_start`, `inspect_effect`,
`prepare_effect`, `commit_effect`, `flush`, `on_complete`, or `close` against
the configured destination. A virtual sink adapter must cover ordinary,
diversion, and error sinks. The final comparison checks for extra **and**
missing members. Audit export is a separate sink effect and is refused unless
the run explicitly configures a safe isolated target in a future contract.

CLI and programmatic admission happens before output-directory creation,
remote secret resolution, dependency runs,
collection probes, plugin construction, plugin lifecycle, and tracing or
telemetry startup. Nested dependencies require their own mode-bound source
runs; absent that binding, the parent invocation is refused. Runtime call
boundaries also check mode, since preflight cannot make a newly added SDK path
safe by assumption. A refusal identifies the unsupported capability and
performs no live provider or sink effect.

Remote Key Vault resolution, dependency runs, collection probes,
commencement gates, audit export, remote telemetry, and concurrency above one
are currently refused for both non-live modes. An admitted built-in adapter
may still refuse an older source run whose audit lacks the transport bytes,
error category, renderer receipt, or unambiguous parent identity it needs.

## Acceptance evidence

An end-to-end replay of a retained run must reproduce source rows and
computed sink-effect evidence with provider and sink spies that fail on any
contact. A nonexistent `replay_from`, purged payload, graph drift, duplicate
request from two nodes, original error, and unsupported SDK plugin must each
refuse before live egress. Verify needs durable exact-match and mismatch
examples; a mismatch must change the terminal run and CLI verdict. Each sink
class needs a negative-control target proving no file, database, or remote
write in either mode. The broad Python and PostgreSQL gates are required for
release integration because the implementation touches shared runtime and
Landscape persistence.
