# ADR-023: Custom Python Static Analyzer for ELSPETH-Specific CI Invariants (the `elspeth-lints` Package)

**Date:** 2026-05-19
**Status:** Accepted
**Deciders:** ELSPETH maintainer
**Tags:** cicd, static-analysis, custom-tooling, elspeth-lints, refactor

## Context

ELSPETH's CI originally enforced a growing set of project-specific invariants through bespoke Python scripts under `scripts/cicd/enforce_*.py`. As of 2026-05-19 the inventory was 14 enforcement scripts (8,758 LOC), covering six conceptual categories that now map to `elspeth-lints` rule families:

| Category | Representative gates | What it enforces |
|---|---|---|
| **Trust-tier** | `trust_tier.tier_model` | `.get()`/`hasattr()`/`getattr()` on typed dataclass fields, 4-layer import discipline, defensive-pattern detection at trust boundaries |
| **Immutability** | `immutability.freeze_guards`, `immutability.frozen_annotations` | `freeze_fields()` contract, `frozen=True, slots=True` annotations on dataclasses with container fields |
| **Audit evidence** | `audit_evidence.tier_1_decoration`, `audit_evidence.guard_symmetry`, `audit_evidence.gve_attribution` | Decorator presence and kwarg shape at audit-emission call sites |
| **Plugin contract** | `plugin_contract.component_type`, `plugin_contract.options_metadata`, `plugin_contract.plugin_hashes` | Required class annotations, options-metadata schema, declared-vs-computed plugin hash manifests |
| **Composer** | `composer.catch_order`, `composer.exception_channel` | Except-handler ordering, composer exception-routing invariant |
| **Manifest** | `manifest.contract_manifest`, `manifest.symbol_inventory`, `manifest.test_to_source_mapping` | Recompute a manifest from source, compare to checked-in declaration; symbol-inventory and test-to-source-mapping invariants |

The enforcement scripts grew organically. A structural review on 2026-05-19
identified that the 14 scripts should consolidate into a single
`elspeth-lints` package with a rule registry, shared CLI, SARIF output, and a
findings-parity contract. **That consolidation has since landed**. The last
script outside the package was ported as
`contract_invariants.adapter_method_budget` and deleted
(`elspeth-3575a7f15d`, commit `7882a127d`); no `enforce_*.py` scripts remain.

This ADR records the prior, more fundamental question: **should ELSPETH be writing this enforcement in custom Python at all, or porting the rules to an off-the-shelf static analyzer?**

The four candidates considered were CodeQL (already in the toolchain via `.github/workflows/codeql.yaml`), Semgrep, ast-grep, and continuing with custom Python in the consolidated `elspeth-lints` package. External reviewers landing in `scripts/cicd/` should be able to find the answer to "why didn't you use CodeQL?" without having to ask. The absence of this ADR was itself a structural gap, identified in the consolidation epic.

## Considered Options

| Option | Fit for ELSPETH-specific invariants | Main benefit | Main cost |
|---|---|---|---|
| **CodeQL custom queries** | Partial. Strong for inter-procedural taint and generic security queries; weak for project-specific dataclass, Pydantic, manifest, and allowlist semantics. | Already integrated with GitHub Code Scanning and the Security tab. | High authoring/debugging friction, database-build workflow, team has no QL fluency, and manifest-class rules still need custom Python. |
| **Semgrep** | Partial. Good for many AST pattern rules; weak for computed-manifest parity and ELSPETH-specific Python object semantics. | Faster rule iteration than CodeQL and broad ecosystem familiarity. | Would still require custom Python for manifest rules, creating a two-toolchain maintenance surface. |
| **ast-grep** | Partial. Useful for syntax-pattern matching; weak for semantic rules involving project helpers, manifests, fingerprints, and allowlist lifecycle. | Lightweight and fast for local AST shape checks. | Same split-toolchain problem as Semgrep, with less direct fit for Python-specific semantic helpers. |
| **Custom Python via `elspeth-lints`** | Full fit for the current rule set. Can reuse Python `ast`, project-specific helpers, YAML manifests, fingerprints, and existing tests. | Preserves existing investment, supports incremental pre-commit naturally, and can model ELSPETH-specific invariants directly. | ELSPETH owns analyzer maintenance, false-positive economics, SARIF integration, and lifecycle discipline. |

## Decisions

### D1. Custom Python (`elspeth-lints`) is the chosen tooling for ELSPETH-specific CI invariants

**Decision:** All ELSPETH-specific CI invariants — the six categories above — will be implemented as Python rules under a single `elspeth-lints` workspace package. We will NOT port them to CodeQL, Semgrep, or ast-grep.

**Why custom Python:**

* **Project-specific semantic abstractions.** The invariants we enforce reach into Python concepts the off-the-shelf tools don't model well — Pydantic field metadata, `@dataclass(frozen=True, slots=True)` discipline, the `freeze_fields()` contract from `core/contracts/freeze.py`, decorator-based audit emission with specific kwarg shapes, and class-attribute conventions on plugin base classes. CodeQL's Python pack abstracts AST nodes but does not currently expose dataclass-field-aware semantics. Re-deriving these in QL would mean rebuilding what `ast` + project-specific helpers give us natively.
* **Manifest-class rules require project-specific cross-artifact recomputation.** `plugin_contract.plugin_hashes`, `manifest.contract_manifest`, `manifest.symbol_inventory`, and `manifest.test_to_source_mapping` recompute a manifest from source and compare it to a checked-in YAML/Python declaration. General-purpose analyzers can inspect some of those files, but do not natively replace this project-specific equivalence check. Even if we ported the AST rules, the manifest rules would stay in custom Python — committing us to a two-toolchain world for no net simplification.
* **Existing investment is paid for.** Fourteen working enforcement scripts,
  regression coverage, and an allowlist mechanism with per-file expiry already
  existed when this decision was taken. Re-implementing them in QL would
  rebuild working project-specific enforcement without closing a capability gap.
* **Local authoring fit.** Python rules use the repository's existing language,
  fixtures, and pytest workflow. The CodeQL CLI and query workflow add a second
  authoring and debugging environment for bespoke per-rule iteration.
* **Pre-commit analysis is simple in Python.** Python rules can walk the changed
  files directly without a separate analysis database in the local hook.
* **Additional dependency avoided.** CodeQL introduces a separately licensed
  CLI and a GitHub-governed toolchain. The custom Python implementation has no
  equivalent external toolchain dependency.

**Why not Semgrep or ast-grep specifically:** they would handle the AST pattern rules well, but they don't fit the manifest-class rules at all. Adopting either would commit us to running TWO toolchains (Semgrep + custom Python for the manifest stuff) — worst of both worlds.

### D2. CodeQL stays in the toolchain as a complement, not as a replacement

**Decision:** `.github/workflows/codeql.yaml` continues to run on its configured
schedule. CodeQL covers CWE-tagged vulnerability patterns and inter-procedural
taint analysis that the upstream Python pack models. CodeQL findings appear in
GitHub Code Scanning; `elspeth-lints` findings are currently retained as CI
SARIF artifacts.

**Why complement rather than choose:** the current responsibilities are
distinct enough that retiring either would lose useful coverage. Custom Python
covers ELSPETH-specific invariants; CodeQL covers general Python vulnerability patterns.

### D3. The acceptance of a "bespoke" reviewer-perception cost is explicit

**Decision:** We accept that some external reviewers will start from "why didn't you use CodeQL?" The cost is real; we judge it lower than the cost of either porting the existing rules to a foreign toolchain or losing the project-specific expressiveness that makes the rules write-able at all.

**Why acceptable:** the perception cost is one ADR-read away from being addressed. Reviewers who dismiss custom analyzers without reading the rationale aren't going to give the project's other architectural choices fair hearing either; reviewers who do read the rationale find the manifest-class-rules argument and the team-fluency argument compelling. The risk is bounded and the mitigation (this ADR, plus the rule-taxonomy rationale doc tracked at filigree `elspeth-797cac825e`) is straightforward.

### D4. Future taint analysis is a planned direction; this ADR does not preclude it

**Decision:** ELSPETH maintainers anticipate adding taint analysis to the enforcement set at some future point (operator-stated 2026-05-19; no committed timeline). When that work begins, the rules that need inter-procedural data-flow MAY be written in CodeQL — that is the *right* tool for that job, and this ADR does not commit ELSPETH to writing every rule in custom Python forever. The decision recorded here is about the *current* invariants (D1's six categories), not about every rule the analyzer set will ever contain.

**Why this matters in the ADR rather than as a deferred consideration:** it explicitly carves out the future-taint case from any reading of this ADR as "Python-purist." The pragmatic principle is: use the tool that fits the rule. The ADR commits us to that principle, not to a specific tool.

**What this means concretely when taint work begins:**

* New CodeQL queries land under a new path (e.g. `.github/codeql/custom-queries/`) and feed into the existing `codeql.yaml` workflow alongside the standard query suite
* They do NOT replace any rule already in `elspeth-lints`; they cover net-new invariants that custom Python can't reach
* The mixed-toolchain world is acknowledged and accepted at that point. Both
  analyzers emit SARIF; their publication destinations and rule taxonomies stay explicit
* A new ADR documenting the introduction of taint queries supersedes the relevant section of this one if a different toolchain choice is made then

### D5. The `elspeth-lints` package is workspace-only, not PyPI-published

**Decision:** `elspeth-lints` ships as a workspace package in the monorepo, installed via `uv pip install -e ./elspeth-lints`. It is NOT published to PyPI.

**Why workspace-only:** publishing implies semver discipline, breaking-change shims, dependency management for external consumers, and documentation overhead none of which is justified before ELSPETH itself has shipped. The package boundary exists to enforce internal modularity (rule registry, shared CLI surface, fixture harness), not to deliver an external product. If post-release we identify other projects that want to adopt some of the rule machinery, a future ADR will document the publishing decision then.

### D6. CodeQL and `elspeth-lints` both emit SARIF

**Decision:** Both analyzers emit SARIF. CodeQL uploads to GitHub Code Scanning;
the current CI retains `elspeth-lints` SARIF as the `elspeth-lints-sarif`
workflow artifact. If `elspeth-lints` is uploaded to Code Scanning later, it
must use a distinct category so the finding sources remain distinguishable.

## Revisit Triggers

Re-open this ADR if one of these conditions becomes true:

* A new ELSPETH invariant requires inter-procedural taint/data-flow analysis that cannot be expressed cleanly with the `elspeth-lints` rule framework.
* CodeQL's Python pack gains first-class dataclass/Pydantic semantics that substantially reduce the custom code required for ELSPETH's trust-tier or plugin-contract rules.
* Semgrep or ast-grep gains a manifest-comparison and allowlist-lifecycle surface strong enough to replace both the AST-pattern rules and the manifest-class rules.
* `elspeth-lints` false-positive maintenance cost grows faster than rule-quality fixes can reduce it for two consecutive audit cycles.
* ELSPETH decides to publish the analyzer outside the monorepo, which would introduce external compatibility and versioning obligations not covered by this ADR.

## Consequences

### What this commits us to

* **Maintaining a static analyzer is on us.** Custom Python means custom
  maintenance: when a rule produces false positives or an allowlist decays, we
  own the remediation. The tracked `cicd-allowlist-audit` skill defines the
  current audit procedure.
* **`elspeth-lints` becomes a load-bearing internal tool.** It runs in pre-commit and PR CI on every code change. Its reliability is a contributor-experience concern. The package gets the test discipline, fixture coverage, and rule-author guide that any internal tool of that exposure deserves.
* **We lose CodeQL's data-flow library.** If a new invariant requires inter-procedural taint tracking *before* the planned taint-analysis work begins, we'll feel this — we'd either write a one-off CodeQL query for that rule (per D4's principle) or defer until the planned taint work catches up. Acknowledged cost.
* **External reviewers may raise the "why not CodeQL" question.** D3 accepts this. This ADR is the answer.

### What this defers

The consolidation work (rule registry, SARIF emission, parity harness,
per-category ports, meta-CI gate, ADR-name rename, pre-commit incremental split,
CI-graph collapse, and rule-taxonomy rationale) has landed, with the final
residual closed at `elspeth-3575a7f15d` (2026-08-06). The taint-analysis future
direction (D4) has no committed timeline; when it begins, a new ADR will record
the chosen toolchain for those specific rules.

### Operator actions

None at ADR-acceptance time. The consolidation work has since landed in full; the final port closed at `elspeth-3575a7f15d` (2026-08-06).

## References

* **Consolidation residual (closed 2026-08-06):** filigree
  `elspeth-3575a7f15d` and commit `7882a127d`.
* **Rule-taxonomy rationale:** [elspeth-lints rationale](../../elspeth-lints/rationale.md).
* **Current SARIF publication:** [CI workflow](../../../.github/workflows/ci.yaml).
* **CodeQL workflow:** [.github/workflows/codeql.yaml](../../../.github/workflows/codeql.yaml)
* **Existing enforcement scripts:** [scripts/cicd/](../../../scripts/cicd/)
* **CICD allowlist lifecycle procedure:**
  `.agents/skills/cicd-allowlist-audit/SKILL.md`
* **Judge-signature and fingerprint workflow:**
  `docs/judge-signature-handoff.md`
