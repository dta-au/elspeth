# ADR-006: Layer Dependency Remediation — Enforcing Strict 4-Layer Import Direction

**Date:** 2026-02-22
**Status:** Accepted
**Deciders:** ELSPETH maintainer
**Review evidence:** Advisory architecture, systems-thinking, and Python-engineering review
**Tags:** architecture, dependency-cycles, contracts, core, engine, layer-enforcement

## Context

ELSPETH has 4 intended layers with downward-only imports:

```
L0: contracts/  →  Type contracts, protocols, schemas (intended foundation)
L1: core/       →  Landscape audit, config, canonical JSON, security, DAG models
L2: engine/     →  Orchestrator, executors, retry, processor
L3: plugins/    →  Sources, transforms, sinks, clients
```

**This layering was never designed — it was emergent.** Packages were organized by topic ("contracts are about types", "core is about infrastructure") rather than by dependency direction ("contracts depends on nothing above it"). The `contracts/__init__.py` docstring claims "LEAF MODULE with no outbound dependencies to core/engine" — this has never been true.

### The Violations

10 runtime layer violations exist, all dating to the initial RC2 commit (`f4f348de`, 2026-02-02). Every one uses a lazy inline import with an apologetic comment:

**Cluster 1 — Canonical hashing (4 violations, MUTUAL CYCLE):**
- `contracts/plugin_context.py:359,441` → `core.canonical.stable_hash, repr_hash`
- `contracts/contract_records.py:138` → `core.canonical.canonical_json`
- `contracts/schema_contract.py:319` → `core.canonical.canonical_json`
- Reverse direction: `core/canonical.py` lazily imports `PipelineRow` from `contracts/schema_contract.py`

**Cluster 2 — URL/Secret handling (3 violations):**
- `contracts/url.py:109` → `core.config._sanitize_dsn` (imports a private function across layers)
- `contracts/url.py:188` → `core.config.SecretFingerprintError`
- `contracts/url.py:233` → `core.security.fingerprint.get_fingerprint_key, secret_fingerprint`

**Cluster 3 — Config (1 violation):**
- `contracts/config/runtime.py:318` → `core.config.ServiceRateLimit`

**Cluster 4 — Expression validation (3 violations):**
- `core/config.py:295,551,639` → `engine.expression_parser.ExpressionParser`

### Systemic Pattern: Shifting the Burden

A systems dynamics analysis identified this as a **"Shifting the Burden" archetype** (Senge, *The Fifth Discipline*). Each lazy import is a symptomatic fix that defers the structural solution:

```
New feature needs hashing     Developer adds lazy       Violation count
in contracts/            →    import workaround     →   increases
      ↑                                                      |
      |                                                      ↓
      |                                                Restructuring
      |                                                effort grows
      |                                                      |
      └──────────────────────────────────────────────────────┘
              "Just add another lazy import,
               restructuring is too big now"
```

The violation set was growing. Without intervention, the window for cheap restructuring was closing.

### Key Findings from Code Analysis

1. **Canonical hashing is a mutual dependency cycle**, not just one-directional. `core/canonical.py` imports `PipelineRow` from contracts, while contracts imports `canonical_json`/`stable_hash` from canonical. Both sides lazy-import from each other.

2. **All contracts-side callers of `canonical_json` only hash plain dicts of primitives** (strings, bools, lists). They never pass PipelineRow, pandas, or numpy objects. This makes extraction clean.

3. **ExpressionParser has zero engine dependencies** — it's pure stdlib (`ast`, `operator`). It lives in `engine/` by topic, not by dependency.

4. **The fingerprint functions are stdlib-only** (`hashlib`, `hmac`, `os`). They're genuine primitives misplaced in `core/security/`.

5. **`_sanitize_dsn` is a private function** imported across layer boundaries — violating both the layer model and Python's convention that underscore-prefixed names are internal.

6. **`ServiceRateLimit` is a Settings-layer Pydantic model** leaking into the Runtime-layer, violating the established Settings→Runtime dataclass pattern.

## Decision

### Maintain Strict 4-Layer Model, Move Code to Match

We maintain the 4-layer hierarchy (contracts → core → engine → plugins) with strict downward-only imports. We relocate misplaced code to its correct layer rather than relaxing the model.

### Phase 1: Move ExpressionParser (eliminates 3 violations)

Move `engine/expression_parser.py` → `core/expression_parser.py`. Pure file move, zero logic changes. Convert 3 lazy imports in `core/config.py` to direct top-level imports.

**Rationale:** ExpressionParser is a security-focused AST validator for config expressions. It belongs in `core/` (L1). Its only dependencies are stdlib (`ast`, `operator`).

### Phase 2: Extract contracts/hashing.py (eliminates 4 violations + breaks circular dep)

1. Remove the `PipelineRow` isinstance check from `core/canonical.py`'s `_normalize_for_canonical()` (~3 lines). Callers already call `.to_dict()` before passing data.
2. Create `contracts/hashing.py` (~15 lines) with primitives-only versions of `canonical_json()`, `stable_hash()`, and `repr_hash()`. Dependencies: `rfc8785` (pure Python, zero transitive deps) + `hashlib` (stdlib).
3. `core/canonical.py` imports FROM `contracts/hashing.py` for the final RFC 8785 serialization step, after its pandas/numpy normalization pass.

**Rationale:** Contracts callers only hash plain dicts — they don't need pandas/numpy normalization. The split is along a clean interface boundary: primitives-only hashing (L0) vs. data-type normalization + hashing (L1).

### Phase 3: Move fingerprint primitives + rewrite DSN handling (eliminates 3 violations)

1. Create `contracts/security.py`. Move `SecretFingerprintError`, `get_fingerprint_key()`, and `secret_fingerprint()` from `core/` to `contracts/security.py`. These are stdlib-only primitives.
2. Replace `_sanitize_dsn` usage in `contracts/url.py` with direct `urllib.parse` implementation. The contracts type only needs password extraction + fingerprinting, not full SQLAlchemy URL normalization.

**Rationale:** Fingerprint functions have no ELSPETH dependencies — they're lower-level than `core/`. The DSN rewrite eliminates a cross-layer import of a private function.

### Phase 4: Add RuntimeServiceRateLimit (eliminates 1 violation)

Add a `RuntimeServiceRateLimit` frozen dataclass in `contracts/config/runtime.py`, following the established Settings→Runtime pattern. Remove the lazy import of `ServiceRateLimit` from `core/config.py`.

**Rationale:** Every other Runtime*Config type has its own dataclass. ServiceRateLimit was the only Settings-layer Pydantic model leaking into the Runtime-layer.

### Phase 5: Enforcement — CI Gate + Decision Protocol

After all violations are fixed:

1. **Strengthen the `trust_tier.tier_model` `elspeth-lints` rule** to fail CI on new upward imports. The allowlist mechanism already exists.
2. **Document the "Violation #11 Protocol"** in CLAUDE.md — a decision protocol for when the next developer needs something from a higher layer:
   - Move the needed code down (if it has no upward dependencies)
   - Extract the primitive portion and move that down
   - Restructure the caller so it doesn't need the cross-layer code
   - **Never** add a lazy import with an apologetic comment
3. **Update `contracts/__init__.py` docstring** to accurately reflect the enforced reality.

**Rationale:** The systems analysis showed that fixing violations without enforcement just resets the addiction loop. The protocol prevents the "Shifting the Burden" archetype from recurring.

### What We Are NOT Doing

1. **No new "primitives" layer.** Adding a 5th layer below contracts/ would create new boundary ambiguity. The systems analysis warned: "the system recreates boundary ambiguity at whatever new boundary you draw."
2. **No Protocol/Dependency Injection for hashing.** There's exactly one correct implementation of `canonical_json`. Protocol indirection adds cognitive overhead without flexibility benefit.
3. **No "foundation tier" model.** Accepting bidirectional deps between contracts and core creates a permission structure that erodes over time. Strict layering is harder to maintain but prevents the entropy.
4. **No re-exports for backward compatibility.** Per the no-legacy-code policy, moved code gets updated imports at all call sites in the same commit.

## Consequences

### Positive Consequences

- `contracts/` becomes a genuine leaf module — testable and importable without pulling in core/engine/plugins
- Static analysis (mypy, import linting) catches layer violations at CI time, not runtime
- Cognitive load decreases — developers can trust the layer model instead of discovering hidden lazy imports
- The "Shifting the Burden" feedback loop is broken by both fixing the stock (10 violations → 0) and adding a balancing loop (CI enforcement)
- `core/canonical.py`'s circular dependency with contracts is severed, making both modules independently refactorable

### Negative Consequences

- ~30 files touched across all phases (mostly mechanical import path changes)
- Two implementations of canonical JSON exist: `contracts/hashing.py` (primitives only) and `core/canonical.py` (with pandas/numpy normalization). Callers must use the right one — but the type signatures make this unambiguous (`dict | list | str | int | float | bool | None` vs `Any`)
- The DSN rewrite in `contracts/url.py` introduces a second URL password-extraction implementation alongside `_sanitize_dsn` in `core/config.py`. Both are small (~30 lines) and the contracts version is simpler (stdlib only vs SQLAlchemy)
- CI enforcement adds friction when legitimate cross-layer needs arise — but the decision protocol provides a structured resolution path

### Neutral Consequences

- `core/canonical.py` shrinks slightly (PipelineRow check removed) and gains an import from `contracts/hashing.py` — dependency direction reverses from incorrect (contracts→core) to correct (core→contracts)
- `core/config.py` loses `SecretFingerprintError` and one validator's lazy import, slightly reducing its 1,600-line scope — directionally helpful for the separate config.py decomposition effort (T23)
- `trust_tier.tier_model` gains new detection capabilities but the allowlist mechanism already handles exceptions

## Alternatives Considered

### Alternative 1: Foundation Tier Model (contracts + core as bidirectional peers)

**Description:** Redefine the layer model from strict 4-tier to "2-tier foundation + 2-tier application." Within the foundation tier (contracts + core), bidirectional dependencies are permitted. The strict boundary moves to foundation↔application (engine, plugins).

**Considered because:** The systems thinker proposed this as a Level 3 (goals) intervention — more honest about the empirical reality than the strict model.

**Rejected because:** It creates a permission structure that erodes over time. Once bidirectional deps are "allowed" within the foundation tier, every new cross-dependency between contracts and core gets justified as "it's all foundation." The Shifting the Burden archetype would shift one level up — from "lazy imports mask violations" to "foundation tier membership masks coupling growth." The strict model is harder to maintain but prevents the entropy.

### Alternative 2: Protocol/Dependency Injection for Hashing

**Description:** Define a `CanonicalHasher` protocol in contracts/, implement in core/. Inject the hasher at construction time rather than importing directly.

**Considered because:** The Python engineer evaluated this as a standard dependency inversion technique.

**Rejected because:** There's exactly one correct implementation of `canonical_json`. Protocol indirection adds boilerplate (protocol definition, injection plumbing, test mocking) without any flexibility benefit. The contracts-side callers don't need polymorphism — they need a deterministic hash of a dict. Direct function call is simpler and equally correct.

### Alternative 3: New "Primitives" Layer Below Contracts

**Description:** Create a new L-1 package (`elspeth.primitives` or `elspeth.foundations`) containing dependency-free utilities: hashing, fingerprinting, canonical JSON.

**Considered because:** Both the architecture critic and Python engineer considered this in their initial analysis.

**Rejected because:** The systems thinker identified this as a structural trap. Adding a new boundary creates new ambiguity ("should this go in primitives or contracts?"). History shows the system recreates the same Shifting the Burden dynamic at whatever boundary is drawn. Only `repr_hash` (3 lines, pure stdlib) truly qualifies as a "primitive" — the rest needs `rfc8785` at minimum. A 5th layer for 3 lines of code is not justified.

### Alternative 4: Defer — Keep Lazy Imports, Fix Later

**Description:** Accept the lazy imports as "messy but functional" and prioritize feature work.

**Considered because:** The violations cause no runtime errors. The lazy imports work.

**Rejected because:** The systems analysis showed this IS the symptomatic
solution driving the Shifting the Burden loop. Each additional violation
increases the eventual restructuring cost, so deferral would make the same
correction harder without adding product value.

## Related Decisions

- ADR-005: Declarative DAG Wiring — establishes `input:`/`on_success:` naming that interacts with ExpressionParser validation
- ExpressionParser move — Phase 1 of this ADR
- MaxRetriesExceeded/BufferEntry move — complementary TYPE_CHECKING violation fixes
- `config.py` decomposition — depends on Phases 1 and 3 of this ADR

## References

- Decision and implementation history: commit `fc189fe9d`
- Senge, Peter M. *The Fifth Discipline* (1990) — "Shifting the Burden" archetype (Chapter 6)
- Meadows, Donella. *Thinking in Systems* (2008) — leverage point hierarchy (Chapter 6)
- Git blame evidence: All 10 violations trace to RC2 initial commit `f4f348de` (2026-02-02) or within days of it

## Notes

### Verification Criteria for Completion

After all 5 phases:
- `grep -rn 'from elspeth.core' src/elspeth/contracts/` returns 0 results (excluding TYPE_CHECKING blocks)
- `grep -rn 'from elspeth.engine' src/elspeth/core/` returns 0 results (excluding TYPE_CHECKING blocks)
- `trust_tier.tier_model` CI gate passes with no allowlist entries for layer violations
- `contracts/__init__.py` docstring accurately states "leaf module with no runtime dependencies on core/engine/plugins"
- The CI-equivalent full test suite passes
- mypy passes with no new `# type: ignore` additions

### Implementation Ordering

The phases must be executed in dependency order within each cluster, but clusters are independent:

```
Phase 1 (ExpressionParser) ─────────────────────────┐
Phase 2 (contracts/hashing.py) ─────────────────────┤
Phase 3 (fingerprint + DSN) ────────────────────────┼──► Phase 5 (CI enforcement)
Phase 4 (RuntimeServiceRateLimit) ──────────────────┘
```

Phases 1-4 have no inter-dependencies and can be parallelized. Phase 5 must wait for all 4.

### Interaction with Other RC3.3 Work

- **088f8b (Phase 2) and T17 (PluginContext split)** both touch `contracts/plugin_context.py`. Phase 2 should complete first (it's Phase 1 priority; T17 is Phase 3).
- **Phase 3 and T23 (config.py decomposition)** — Phase 3 removes code from config.py. T23 splits what remains. Dependency is wired: T23 depends on Phase 3 completion.
- **Phase 2 step 1 (remove PipelineRow check)** — verify no callers pass PipelineRow directly to `canonical_json()` without `.to_dict()`. Contracts callers confirmed clean; check engine/core callers during implementation.
