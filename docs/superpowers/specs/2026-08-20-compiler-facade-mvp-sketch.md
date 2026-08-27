# Compiler facade — minimum viable path to composer / compiler / executor

**Date:** 2026-08-20
**Status:** Sketch (not a plan; no tracker yet)
**Context:** `docs/product/roadmap.md` "Later" bullet — *Compiler facade — seal YAML
+ composer input into one compiled, secret-safe pipeline artifact bound to
Landscape provenance; run web and CLI execution from the verified artifact
instead of reparsing YAML.* · tracker: none yet.

## The thesis

ELSPETH already computes every pin a compiled artifact would need. It computes
them at `begin_run` — *after* the decision to execute — and writes them into the
`runs` row. That makes them **provenance about what ran** rather than a
**contract about what will run**.

The compiler facade is mostly a relocation of existing machinery across that
line, plus an emit and a gate. This sketch scopes the smallest version that
makes the three-system split real.

## What already exists

| Piece | Location | Note |
|---|---|---|
| Common IR | `ElspethSettings` | Both front-ends lower to it |
| Composer front-end | `CompositionState` → `yaml_generator` → `load_settings_from_yaml_string` | |
| YAML front-end | settings file → `load_settings` | |
| Compiler body | `web/execution/validation.py::validate_pipeline` | *"Dry-run validation using real engine code paths. Calls the same functions as `elspeth run`"* |
| Shared assembly | `orchestrator/preflight.py::assemble_and_validate_pipeline_config` | Docstring says it *mirrors* `orchestrator/core.py` — see Stage 4 |
| Round-trip loader | `config.py::load_settings_from_config_dict(..., expand_env_vars=False)` | Refuses host-env expansion **by default** |
| Canonical body | `config.py::resolve_config` | `model_dump(mode="json")` + secret fingerprinting |
| Deferred secrets | `core/secrets.py::resolve_secret_refs`, marker `{"secret_ref": "NAME"}` | Late resolution, returns `(config, resolutions)` |
| Secret fingerprint | `contracts/secrets.py::ResolvedSecret.fingerprint` | Already carried per resolution |
| Config identity | `stable_hash(resolve_config(settings))` | Stored as `runs.config_hash` |
| Topology identity | `core/canonical.py::compute_full_topology_hash(graph)` | |
| Contract identity | `contracts/runtime_val_manifest.py` | Bytecode-level `implementation_hash` per declaration contract |
| Plugin identity | `web/plugin_policy/compiler.py` | version + `sha256:` source hash regexes |
| Model-catalog identity | `begin_run(openrouter_catalog_sha256=..., openrouter_catalog_source=...)` | Required argument |
| Signed envelope pattern | `web/shareable_reviews/signer.py::ShareTokenSigner` | HMAC-SHA256 + expiry + nonce + content-addressed `payload_digest` |

### The admission-gate pattern is already built

Three of four execution entry paths **verify** an identity hash and refuse:

- `orchestrator/join_admission.py:124` — joiner's resolved settings hash must
  equal `runs.config_hash`, else `JoinRefusedError`.
- `core/checkpoint/compatibility.py:66` — resume refuses on
  `full_topology_hash` mismatch.
- `orchestrator/export.py:325` — export reconciliation refuses on
  `config_hash` mismatch.

The fresh-run path (`cli.py::run`) computes `config_hash` and **records** it.
It does not compare, because there is nothing to compare against. The artifact
is the missing operand, not the missing gate.

## Separate integrity from authority

Two distinct properties, deliverable in that order:

1. **Integrity (MVP)** — hash-gating proves *this artifact matches what was
   compiled*. Needs no key and no custody model.
2. **Authority (later)** — a signature proves *a trusted party compiled it*.
   Only matters once a bundle crosses a trust boundary.

Shipping integrity first removes key custody, rotation, and the no-dual-key
window (`ShareTokenSigner`'s own documented limitation) from the critical path.
When signing does land, the key is deployment/operator-held and follows the
[O1] custody posture: agents never hold it.

## The artifact

`CompiledPipeline`, a new L0 contract. JSON, plain data, diffable.

```
version            closed int; unknown versions reject (no silent forward-compat)
body               settings model dump with secret positions carrying
                   {"secret_ref": NAME} markers rather than values
manifest
  config_hash              stable_hash over the canonical body
  full_topology_hash       compute_full_topology_hash(graph)
  secret_bindings          [{name, scope, fingerprint}] from compile-time resolution
  plugin_bindings          [{plugin_id, version, source_hash}]
  runtime_val_manifest     build_runtime_val_manifest()
  openrouter_catalog_sha256 / _source
  reproducibility_grade    the claim this bundle makes
  elspeth_version, canonical_version
```

**The body keeps references, never values.** This is not a compromise on the
"secrets baked in" idea — it is strictly better: the artifact stays storable in
Landscape and the payload store, every run still emits its own auditable
resolution event, vault authority stays with the deployment
(`ELSPETH_KEYVAULT_ALLOWED_VAULT_URLS` is deployment-provisioned, *never*
pipeline YAML), and rotating a secret does not invalidate every artifact.

## Stages

### Stage 1 — Canonical body (no new surface)

The blocker for a round-trip is that `resolve_config` fingerprints *expanded*
secret values, destroying the reference. `_fingerprint_config_for_audit`
already walks the secret-bearing positions: `landscape.url`, `sources.*`,
`source.options`, `sinks.*` (including database `url`), `transforms[*].options`,
`aggregations[*].options`, and `telemetry.exporters[*]`.

Add a second mode to that walker: emit `{"secret_ref": NAME}` instead of a
fingerprint. Same tested walker, one branch.

**Done when:** `load_settings_from_config_dict(canonical_body(s))` reproduces
`s` and the same `config_hash`, over the whole `examples/` corpus.

**The emitter must fail closed.** A missed position silently ships a plaintext
secret into the artifact, and "enumerate every position carefully" is the
weakest possible control for the highest-consequence step in this design. The
precedent is in the same function: `_fingerprint_config_for_audit` already
raises `SecretFingerprintError` when it finds secrets with no fingerprint key,
unless `ELSPETH_ALLOW_RAW_SECRETS` is set.

The canonical-body emitter should refuse the same way — **no bundle is emitted
if any value at a walked position is neither a `{"secret_ref": …}` marker nor a
known ref.** That turns "did we enumerate every position?" from an audit
question into a compile-time refusal, and it is what makes the bundle safe to
store in Landscape and the payload store, which this design assumes throughout.

**Known limitation, not closed by the MVP.** The walker is a hardcoded path
list. Its coverage is already wider than its own docstring claims (the
docstring omits `sources` plural and `telemetry`, both of which the code walks),
which is itself evidence that the list drifts. Enumerating the top-level blocks
it skips today (`gates`, `coalesce`) only finds today's gaps — the real failure
mode is a *future* plugin whose options carry a credential at a position nobody
added to the walker. Nothing currently fires when that happens. The fail-closed
emitter above contains the blast radius (an unrecognised value at a *walked*
position refuses), but it cannot see a position that is not walked at all.

Deriving secret-bearing positions from plugin option declarations rather than a
path list is the real fix and is deliberately out of MVP scope. Until then this
is a stated limitation of the artifact, not a solved problem.

### Stage 2 — Compiler emits

`elspeth compile --settings x.yaml -o x.epb`

Runs the existing `validate_pipeline` path. On green, serialises
`CompiledPipeline`. On red, exits non-zero with today's diagnostics unchanged.
`elspeth validate` becomes `compile` without `-o`.

Nothing in the runtime consumes the bundle yet. It is still worth shipping
alone, but only because two things can read it immediately: `diff` over two
bundles gives a reviewable answer to "what actually changed between these two
pipelines" that YAML diffs do not (defaults are resolved, ordering is
canonical), and CI can archive a bundle per green validation. If neither of
those is wanted, Stage 2 should be landed together with Stage 3 rather than
shipped on its own.

### Stage 3 — Executor consumes and gates

`elspeth run --bundle x.epb --execute`

**Two gates, not one — the ordering is load-bearing.** `join_admission` gets to
compare before doing any work because its operand is a DB row. Here the operand
is the file the executor is about to act on, and `full_topology_hash` needs a
built graph, which needs plugin instantiation. Collapsing the gates would mean
instantiating plugins against unverified config. So:

1. Reject unknown `version`.
2. **Gate A — before any instantiation.** Recompute `config_hash` over the
   canonical body; refuse on mismatch. Nothing has been constructed yet.
3. `resolve_secret_refs(body, ...)` → refuse on any fingerprint that differs
   from `manifest.secret_bindings`.
4. `load_settings_from_config_dict(resolved, expand_env_vars=False)`, then
   instantiate plugins and build the graph.
5. **Gate B — after graph build.** Recompute `full_topology_hash`; refuse on
   mismatch.
6. Hand off to the existing run path; `begin_run` records the bundle's identity
   rather than deriving its own.

Both refusals reuse `join_admission`'s refusal shape and message discipline. An
implementer must not merge A into B: Gate A is what keeps step 4 from running
on config the bundle does not vouch for.

`--settings` keeps working unchanged. Two entry points, one execution core.

**This is the point at which "compiled clean" becomes a real claim.**

### Stage 4 — Composer emits the same artifact

The composer's run path produces a `CompiledPipeline` and hands *that* to the
execution service, instead of passing settings and graph objects. Both
front-ends now produce one artifact type consumed by one executor.

Collapse the three assembly sites at the same time —
`assemble_and_validate_pipeline_config` currently describes itself as mirroring
`orchestrator/core.py` and `web/execution/service.py`. One emit path forces one
assembly.

## Explicitly not in the MVP

- **Signing.** Integrity first; see above.
- **Cross-host portability.** A bundle run on a different machine needs plugin
  *code* identity verified, not just recorded. `runtime_val_manifest` and the
  plugin-policy `sha256:` hashes give the data; nothing gates on them yet.
  Until that gate exists, a bundle is host-local.
- **Bundle-only execution.** `--settings` stays.
- **Landscape provenance binding** beyond `config_hash`.

## What a bundle can and cannot claim

A verified bundle claims **admissible**, not **works**: this executor can run
exactly this graph, with exactly these plugins, against exactly these
credentials.

It cannot claim more, for two reasons already recorded in the architecture:

- ADR-040 §1 names *three* validation surfaces. The third — per-row executor
  enforcement — is not static and does not go away.
- `ReproducibilityGrade` already encodes the ceiling: `REPLAY_REPRODUCIBLE`
  means at least one node is nondeterministic.

Stamping the grade into the manifest makes the claim self-describing.

## What does not change

**ADR-040 stays intact.** Its §2 is explicit: *"Global equivalence is
explicitly not the target — Stage 1 is a fast, explainable authoring aid, not a
re-implementation of the engine."* The compiler facade makes **Stage 2
durable**. Composer Stage 1 and its parity ratchet (elspeth-2ed41f0a4a) are
unaffected and should not be retired.

The composer invariants are unaffected: the compiler consumes what the planner
authored, and rejects. It never authors.
