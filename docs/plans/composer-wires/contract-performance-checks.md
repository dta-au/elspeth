# Contract performance checks

> **Resumption reference, promoted 2026-09-12.** The [campaign plan](../2026-09-08-composer-wires-campaign.md) controls current custody, scope, order and authorization.
> This document preserves September 10 preparation; source locations, measured counts, tests and active-writer restrictions describe that historical snapshot.
> Earlier scratch records named below are optional historical evidence, not prerequisites to recover from temporary storage. Re-measure the consolidated source before implementation.
> The current request covers consolidation and planning only. Its consolidation commits supersede the historical commit ban for that scope; this document does not initiate implementation or authorize provider calls, deployment, store deletion or signing.

Preparation from restricted-open-shape-assessment.md, restricted-open-shape-systems.md and model-admission-implementation-charter.md. No mutable source/gate imports, provider requests, repository edits or benchmarks were performed. All overhead below is a hypothesis to measure after the implementations freeze. Precise admission and disclosure remain requirements; no quota or latency argument weakens them.

## Likely mechanisms and reuse

| Mechanism | Where to inspect and measure | Safe reuse boundary |
| --- | --- | --- |
| Rebuilding model/schema validators on every call | Input model/adaptor construction; producer response-contract lookup | Define owned models once. If TypeAdapter is actually needed, construct one per static producer family/outcome contract beside its declaration, after referenced types resolve. Reuse the compiled adapter, never the previous validation verdict for new input. Do not introduce TypeAdapter merely to add a cache. |
| Trying every arm of a large union | Heterogeneous discovery admission, especially invalid payloads | Select the declared tool contract and success/failure family first. Reuse existing shared families such as plugin inventory. Keep small real per-tool alternatives; avoid a global trial-and-error union. |
| Repeated traversal/allocation | Deep thaw/freeze, model construction, model_dump and json.dumps of inventory lists or nested schema/config literals | Measure traversals separately. Pass the admitted owned model forward and consume typed fields; avoid dumping to a mapping only to validate it again. Preserve exact existing serialization/omission semantics. Do not assume frozen outer objects imply recursively immutable BaseModel leaves. |
| Duplicate schema work | Plugin-contract projection, canonical JSON for byte budgeting, final wire JSON | Count each separately. Existing canonical budget/hash serialization and ordinary wire serialization have different contracts; do not replace one with the other. Reuse immutable schema-derived work only under its existing catalog/snapshot identity. |
| Repeated complete argument admission | Public handler -> candidate builder -> advisor handoff | Validate complete original input at the real public authority boundary, before operational effects, and pass the owned result to an internal implementation. Never validate only a reconstructed subset. Direct public/MCP/candidate routes must retain their own real admission until a common boundary demonstrably owns them. |
| Large failure construction | Wrong scalars, extra root keys, deeply nested invalid leaf, wrong-tool result | Measure error discovery separately from safe ToolArgumentError/ARG_ERROR projection. Do not format raw Pydantic error input or expose content to gain a shortcut. Internal response corruption remains a loud failure. |

Static adapters belong with producer contracts, referenced through the declaration registry. They must not capture session engines, current state, user policy, remaining budget, or provider context. Reusing compiled contracts across requests is appropriate; caching those request-specific decisions is not.

A cached discovery payload still traverses current tool/outcome admission and current surface disclosure on reuse. Restricted state comes from the supplied current projection; successful preview remains refused; schema projection availability and the remaining aggregate budget are checked each time. A cache hit must not reuse an unrestricted wire response on a restricted surface or resurrect a stale state/version. Existing discovery-cache eligibility remains authoritative; this exercise adds no caching of mutations or advisor side effects.

## Bounded local benchmark after freeze

Use a temporary script and captured safe fixtures, not a new benchmark framework. Freeze both integrated source and exact pre-change comparison revision; record hashes, interpreter, dependency versions and both source-root import paths. Run each revision in its own fresh process, with LITELLM_MODE=PRODUCTION and LITELLM_LOCAL_MODEL_COST_MAP=True. Use local fixtures/stubs only. If another suite is saturating the host, defer measurement rather than infer a regression from contention.

Fixture matrix:

- Fixed successful roots: audit information and grammar; absent-data outcome where genuinely supported.
- Lists: real PluginInventory and list_blobs/list_secret_refs output, empty, typical fixture and the producer's existing supported upper/large representative size. Keep list roots; do not wrap them for convenience.
- Dynamic leaves: both list_models variants with unusual provider names; a real bounded PlannerPluginContract with nested legal defaults/properties; plugin assistance before/after configuration; source inspection with unusual observed column names. Record item count, nesting depth and serialized byte length.
- Restricted outcomes: full context and matched node/output projections; preview refusal; schema projection refusal and budget equal/over boundaries; current shared argument/error outputs after error-twin integration.
- Invalid contract cases: extra root key, wrong-tool otherwise-valid response, wrong nested scalar, arbitrary model/object; input bool/numeric-string/null/extra-key rejection plus one valid nested pipeline and a rejected object-string. Include the public advisor admission path with no-op provider/budget spies proving invalid input never reaches either.

For valid fixtures, prove byte equality and disclosure invariants before timing. For tightened invalid input, compare correct rejection behavior, not obsolete acceptance bytes. Independently assert state/version stability, safe failure content, no operational writes and no provider calls on rejection paths. Re-run these semantic checks after timing; speed does not certify them.

Measure (a) process import/adapter initialization, (b) warm adapter admission, (c) projection/thaw/dump/encoding phases, and (d) the complete local public admission or discovery egress path. Keep fixture construction and telemetry printing outside warm timing; include all actual validation/projection work inside it. Use perf_counter_ns, a short warmup, then 20 bounded batches per fixture with enough iterations to make each batch measurable (cap overall run around two minutes per revision). Alternate before/after process order and repeat three times if results suggest regression. Report median and spread of batch-average latency, absolute difference and ratio; do not label batch averages as request p95. Measure peak allocations separately with tracemalloc so instrumentation does not contaminate timing.

Optimization trigger: a repeatable increase traced to a named phase, especially duplicate compilation/traversal or scaling worse than payload size. Report startup cost separately from warm cost. There is no invented millisecond threshold and no inference about provider/network latency from these local measurements. Optimize avoidable work, then repeat the same fixtures and correctness checks. Do not skip validation, relax exact roots, suppress failure reporting, cache disclosure verdicts or hide useful teaching to improve a timing number.
