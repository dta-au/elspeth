# 09 — Secure development lifecycle

**Status:** product process baseline complete; independent assessment and
deployment change control open · **Reviewed against:** `release/0.8.1` @
`004c0eee0` (2026-09-30) · **Owner:** ELSPETH maintainer

Shows how code is written, reviewed, tested and gated before it reaches a
release. For each gate it records what the gate checks, where it runs, and
whether a failure blocks the merge. Because ELSPETH's code is generated and
reviewed by AI ([01 § 8](01-system-overview-and-boundary.md#8-maturity-and-assessment-constraints)),
this document carries more weight than usual; § 5 sets out how the process
compensates.

Facts about files were checked against the tree at the commit above. Live
GitHub settings (rulesets, repository and Actions settings) were read on
2026-09-30 with the read-only `gh api` calls listed in § 1.5; nothing was
changed. Items that depend on the deploying organisation are marked
**Deployment record**.

Existing material:

- [CONTRIBUTING.md](../../CONTRIBUTING.md) (especially
  [whole-tree gates](../../CONTRIBUTING.md#whole-tree-gates-and-conventions-you-will-hit))
  and [GOVERNANCE.md](../../GOVERNANCE.md)
- [AGENTS.md](../../AGENTS.md) — the covenant every contributor and agent
  follows, including the
  [test verification policy](../../AGENTS.md#test-verification-policy)
- [CI branch protection runbook](../runbooks/ci-branch-protection.md)
- [ADR-023 Custom Python CI analyser](../architecture/adr/023-custom-python-ci-analyzer.md),
  [ADR-043 Shared development tooling](../architecture/adr/043-project-tooling.md),
  [ADR-046 Audit grade is a product characteristic](../architecture/adr/046-audit-grade-is-a-product-characteristic.md)
- [Judge signature handoff](../judge-signature-handoff.md)
- Workflows in [.github/workflows/](../../.github/workflows/) [EV-004]
- Hooks in [.pre-commit-config.yaml](../../.pre-commit-config.yaml) [EV-005]

## 1. Change control

### 1.1 Branch model

| Branch or ref | Role |
|---|---|
| `main` | Default branch. Receives release lines through pull requests under the live ruleset [EV-601] |
| `release/X.Y.Z` | Integration branch for one release line. Feature branches merge into it by pull request or are committed to it directly |
| `v*` tags | Pushing a version tag is the operator action that publishes a release image (§ 6.2) [EV-625] [EV-626] |
| `dependabot/*` | Automated dependency-update branches, opened as pull requests (§ 2.2) [EV-616] |

CI, CodeQL and an additional judge lint and quality workflow run on every push
and pull request to `main`, `master`, `RC*` and `release/**`. The judge
workflow is advisory by design and is not part of the standard enforcement
package. The redaction gate and the commit-trailer gate run on pull requests
to the same branches [EV-604] [EV-611] [EV-612] [EV-613] [EV-614].

### 1.2 Who may change code

- The repository `dta-au/elspeth` is public and owned by a GitHub
  organisation. Named accounts hold read, write or admin roles; who holds
  them is a **Deployment record** item (§ 1.6).
- ELSPETH runs in **single-maintainer mode**
  ([GOVERNANCE.md § Maintainer continuity](../../GOVERNANCE.md#maintainer-continuity)).
  The required number of approving human reviews is zero, because
  self-review cannot satisfy an approval requirement. Merges to `main` are
  instead protected by mandatory automated gates (§ 2), and release images
  are tied to commits that passed them (§ 6.2) [EV-601] [EV-603] [EV-625].
- GOVERNANCE.md names the settings that switch on when a second independent
  maintainer takes part: one required approval, stale-review dismissal,
  last-push approval, required conversation resolution and an ownership map
  for security-sensitive paths [EV-603].
- Release authority sits with the repository maintainer. GOVERNANCE.md
  requires the approver, date, branch or tag, commit, evidence links and
  exceptions to be recorded for a public release [EV-603].

### 1.3 Protection of `main`

Measured from the repository ruleset `main` (id 12348893, enforcement
`active`, target `~DEFAULT_BRANCH`, last updated 2026-09-10) [EV-601]:

| Rule | Setting |
|---|---|
| Branch deletion | Blocked |
| Non-fast-forward (force) pushes | Blocked |
| Pull request before merge | Required; 0 approving reviews; merge, squash and rebase allowed |
| Required status checks | `CI Success`, `CodeQL`, `Check cohort-attribution trailers on PR commits`, `redaction-gate` |
| Branch up to date before merge | Required (`strict_required_status_checks_policy: true`) |
| Automated code review | Copilot code review requested on each push to a pull request (advisory, not a required check) |
| Bypass actors | None; the ruleset reports `current_user_can_bypass: never` for an admin account |

`CI Success` is an aggregate job. It fails unless every one of ten CI job IDs
succeeded (§ 2.1), so one required check covers all of them and a renamed job
cannot silently drop out of protection [EV-604].

The ruleset applies to the default branch. Code on a release branch reaches
`main` only through a pull request that meets this ruleset, and an image is
published only for a commit whose required checks passed (§ 6.2) [EV-601]
[EV-625]. The
corresponding governance and repository-setting risks are handled through the
[risk register](15-risk-register.md).

### 1.4 CI execution boundary

- **Public-repository guardrail.** Pull-request workflows run code from
  unmerged branches, so every pull-request job runs on a GitHub-hosted runner.
  Only push, tag, schedule and manual events use the project's self-hosted
  `trusted` runner (`runs-on` expression in each workflow). GitHub does not
  pass repository secrets to workflows triggered from forks [EV-604].
- **Least privilege.** Workflows declare `permissions:`; most jobs hold
  `contents: read` only. Write scopes are limited to the jobs that need them
  (`security-events: write` for CodeQL, `packages: write` and `id-token: write`
  for image publication and signing, `contents: write` for release creation)
  [EV-604].
- **Repository Actions settings** (measured): SHA pinning of actions is
  required; the default workflow token is read-only; workflows cannot approve
  pull requests [EV-602].
- **Pinned actions.** Recursive YAML parsing found 98 `uses:` references in
  nine workflow files. All 98 are third-party actions pinned to a full
  40-character commit SHA, representing 18 distinct actions; there are no
  local or `docker://` references. Before use, the classifier accepted a
  40-character SHA, rejected a tag and a short SHA, and classified local and
  `docker://` samples outside the third-party count [EV-605].
- **Pinned tool downloads.** actionlint, Terraform and Bicep are downloaded by
  fixed version and checked against a SHA-256 before use. Python dependencies
  install with `uv sync --frozen` (the lockfile), frontend dependencies with
  `npm ci` [EV-604].
- **Secrets.** Which workflow references which secret is set out in
  [07 § 5.2](07-secrets-and-key-management.md#52-ci-secrets). No workflow
  holds the judge-metadata signing key
  ([07 § 5.1](07-secrets-and-key-management.md#51-judge-metadata-hmac-key))
  [EV-610] [EV-617].

### 1.5 How the settings were measured

Run on 2026-09-30 with an authenticated maintainer account. All are GET
requests [EV-601] [EV-602] [EV-612].

| Setting | Command |
|---|---|
| Rulesets | `gh api repos/dta-au/elspeth/rulesets` |
| `main` ruleset | `gh api repos/dta-au/elspeth/rulesets/12348893` |
| Effective rules on a branch | `gh api repos/dta-au/elspeth/rules/branches/main` (and `release%2F0.8.1`) |
| Actions permissions | `gh api repos/dta-au/elspeth/actions/permissions` and `.../actions/permissions/workflow` |
| Private vulnerability reporting | `gh api repos/dta-au/elspeth/private-vulnerability-reporting` (`enabled: true`) |
| Code scanning uploads | `gh api 'repos/dta-au/elspeth/code-scanning/analyses?per_page=3'` |

Re-run these for the release being assessed; repository settings can change
without a commit.

### 1.6 Deployment record — change control

| Item | Value |
|---|---|
| Release and commit assessed; CI run for that commit | DEPLOYMENT-TODO: |
| Accounts holding admin and write roles on the repository, and who reviews that list | DEPLOYMENT-TODO: |
| Who may push version tags and publish images | DEPLOYMENT-TODO: |
| Whether the deploying agency requires independent human review before a release is deployed, and who performs it | DEPLOYMENT-TODO: |
| Change approval for deploying a new release (change board, ticket, approver) | DEPLOYMENT-TODO: |
| Date the settings in § 1.3–§ 1.4 were last re-measured | DEPLOYMENT-TODO: |

## 2. Automated gates

### 2.1 What `CI Success` aggregates

`CI Success` (`ci.yaml`) runs with `if: always()` and fails unless each of
these jobs reports `success` [EV-604]:

| Job | Content |
|---|---|
| `Static analysis` | actionlint, ruff, mypy, contract checks, elspeth-lints rules (§ 2.3) |
| `Test (Python 3.12)`, `Test (Python 3.13)` | Default pytest selection; coverage floors on 3.13 |
| `Testcontainer (PostgreSQL contention proofs)` | `pytest tests/ -m testcontainer -n 0` against real PostgreSQL |
| `Host-runner unit (docker CLI, non-root filesystem)` | Tests that need a Docker CLI or a non-root user; required skips become failures |
| `State-engine catalog and selector validation` | Proof catalogues, plugin lifecycle matrix and documentation links |
| `Azure Container Apps Bicep bundle` | Compiles every Bicep template and parameter file; compiled-template contract test |
| `Dependency and License Audit` | Strict `pip-audit` and GPL/AGPL refusal for the separately locked root and gateway Python graphs; `npm audit` on both npm lockfiles |
| `Gateway (locked tests and image)` | Frozen gateway tests and conformance; real-image build; zero High/Critical Trivy admission; exact revision and UID/GID; read-only external conformance |
| `Frontend E2E (Playwright)` | Browser journeys against a real backend and Chromium |
| `Frontend unit (vitest + typecheck)` | TypeScript type check and vitest |

A source check at `004c0eee0` proves that the aggregate names all ten job IDs,
including the gateway job. This local commit has no GitHub CI run, so the
successful execution of that job and a refreshed live-ruleset observation
remain release evidence to capture; the required check name itself remains
`CI Success`.

A skipped or cancelled job counts as a failure. The `Integration Tests` job
(tests that may call a live model) runs only on pushes to protected
branches, after `Test`. It is not part of `CI Success`, but it fails the CI
workflow run, and image publication after CI on `main` requires that run to
have succeeded (§ 6.2) [EV-604] [EV-617] [EV-625].

### 2.2 Gate table

"Blocks merge" means the gate feeds one of the four required status checks
on `main` (§ 1.3). Pre-commit hooks run on the contributor's machine once
`pre-commit install` has been run; they block the local commit.

| Gate | What it checks | Runs at | Blocks merge to `main`? | Definition |
|---|---|---|---|---|
| actionlint | Workflow syntax and expressions | CI `Static analysis` | Yes (`CI Success`) | [ci.yaml](../../.github/workflows/ci.yaml) [EV-604] |
| Ruff | Lint rules E, F, W, I, UP, B, SIM, C4, DTZ, T20, RUF; formatting | Pre-commit (changed files, check-only); CI `Static analysis` over `src/ tests/ scripts/ examples/ elspeth-lints/src/` | Yes (`CI Success`) | [.pre-commit-config.yaml](../../.pre-commit-config.yaml), [pyproject.toml](../../pyproject.toml) [EV-604] [EV-606] |
| mypy | Strict typing (`strict = true`, `warn_unreachable`, `warn_unused_ignores`, pydantic plugin) over `src/` and `elspeth-lints/src/` | Pre-commit; CI `Static analysis` | Yes (`CI Success`) | as above [EV-604] [EV-606] |
| Contract alignment | Every settings field reaches its runtime counterpart; documented internal defaults; soft-mapping census | Pre-commit (any `src/elspeth` change); CI `Static analysis` | Yes (`CI Success`) | `scripts/check_contracts.py` [EV-606] [EV-607] |
| Composer skill inventory | The composer skill's tool list matches the registered tools | Pre-commit; CI `Static analysis` | Yes (`CI Success`) | `scripts/cicd/generate_skill_inventory.py` [EV-606] |
| `elspeth-lints` rules | Project-specific invariants (§ 2.3) | Pre-commit (per rule, on trigger paths); CI `Static analysis` | Yes (`CI Success`), except `trust_tier.tier_model` (§ 2.4) | [ADR-023](../architecture/adr/023-custom-python-ci-analyzer.md) [EV-606] [EV-608] [EV-610] |
| Lint migration parity | Shadow-mode comparison of migrated lint rules | CI `Static analysis` | Yes (`CI Success`) | `scripts/cicd/parity_harness.py` [EV-604] |
| Python tests (default selection) | Unit, integration, property, invariant and end-to-end tests (§ 3), on Python 3.12 and 3.13 | CI `Test` | Yes (`CI Success`) | [ci.yaml](../../.github/workflows/ci.yaml) [EV-604] [EV-618] |
| Coverage floors | Branch-enabled coverage ≥ 85 % overall; `core/landscape` ≥ 92 %, `core/canonical.py` ≥ 99 %, `engine/orchestrator` ≥ 90 %, `contracts` ≥ 62 % | CI `Test (Python 3.13)` | Yes (`CI Success`) | as above [EV-621] |
| PostgreSQL testcontainer suite | Schema, SQL, locking and deployment acceptance against real PostgreSQL (652 tests, § 3.2) | CI `Testcontainer` | Yes (`CI Success`) | as above [EV-604] [EV-619] |
| Live-provider integration tests | Integration tests with a model API key | CI `Integration Tests`, push only | No — runs after merge; gates image publication (§ 6.2) | as above [EV-604] [EV-617] [EV-625] |
| Frontend unit and types | `tsc` over two projects; 238 vitest files | CI `Frontend unit` | Yes (`CI Success`) | as above [EV-604] [EV-620] |
| Frontend end-to-end | Playwright browser journeys | CI `Frontend E2E` | Yes (`CI Success`) | as above [EV-604] [EV-620] |
| Dependency audit (`pip-audit`) | Known vulnerabilities in the separately locked root and standalone gateway Python graphs, `--strict`, with each root exception justified in the workflow | CI `Dependency and License Audit` | Yes (`CI Success`) | as above; detail in [10](10-vulnerability-and-supply-chain.md) [EV-604] [EV-707] |
| Dependency audit (`npm audit`) | Known vulnerabilities in the frontend and root npm lockfiles, any severity | CI `Dependency and License Audit` | Yes (`CI Success`) | as above; detail in [10](10-vulnerability-and-supply-chain.md) [EV-604] [EV-707] [EV-720] |
| Licence check | Fails on GPL or AGPL dependencies in both locked Python graphs and retains both reports | CI `Dependency and License Audit` | Yes (`CI Success`) | as above [EV-604] [EV-707] |
| Assembled gateway image qualification | Frozen tests and conformance; built-image High/Critical Trivy refusal, exact-revision and fixed UID/GID checks, and read-only external conformance | CI `Gateway (locked tests and image)` | Yes (`CI Success`) | [ci.yaml](../../.github/workflows/ci.yaml), `tests/unit/cicd/test_gateway_supply_chain.py` [EV-604] [EV-617] [EV-722] |
| CodeQL | Python, `security-extended` query suite; tests and lint fixtures excluded; also weekly on schedule | CI `Analyze Python`, push, pull request and Monday schedule | Yes (`CodeQL`) | [codeql.yaml](../../.github/workflows/codeql.yaml), [codeql-config.yml](../../.github/codeql/codeql-config.yml) [EV-612] |
| Composer redaction gate | A change to the redaction snapshot is classified as weakening or strengthening; the matching label, and a rationale for a weakening, are required | Pull request (also re-runs on label and description edits) | Yes (`redaction-gate`) | [composer-redaction-gate.yml](../../.github/workflows/composer-redaction-gate.yml), [policy guide](../guides/redaction-policy-changes.md) [EV-613] |
| Telemetry backfill trailer | Every commit touching a telemetry cohort directory carries its attribution trailer | Pre-commit (`commit-msg` stage); pull request | Yes (`Check cohort-attribution trailers on PR commits`) | [enforce-telemetry-backfill-trailer.yaml](../../.github/workflows/enforce-telemetry-backfill-trailer.yaml) [EV-614] |
| Additional judge lint and quality diagnostics | Rolling 30-day operator-override signal for judged suppressions; judge-quality corpus signal against a live model (trusted pushes only) | Push and pull request | No — advisory by design and outside the standard enforcement package | [enforce-allowlist-judge-gates.yaml](../../.github/workflows/enforce-allowlist-judge-gates.yaml) [EV-611] |
| Trust-tier model | Defensive patterns and upward imports that hide bugs; signed suppressions (§ 2.4) | Pre-commit ratchet; operator verification | Not a CI gate by design ([AGENTS.md](../../AGENTS.md#operator-signature-verification-tier-model-allowlist-signing)); track resulting risk in [15](15-risk-register.md) | `scripts/trust_tier_ratchet.py` [EV-610] |
| Secret scanner | Credential-shaped strings in staged content ([07 § 4.4](07-secrets-and-key-management.md#44-source-control)) | Pre-commit, every commit | Blocks the local commit; track residual source-control risk in [15](15-risk-register.md) | `scripts/git-hooks/pre-commit-secret-scan.sh` [EV-606] |
| File hygiene hooks | Trailing whitespace, final newline, YAML and TOML syntax, files over 1,000 KB, merge-conflict markers, debug statements | Pre-commit | Local only | [.pre-commit-config.yaml](../../.pre-commit-config.yaml) [EV-606] |
| Mutation testing | Whether tests kill injected faults in `core/canonical.py` and `core/landscape/` | Weekly schedule and manual | No — advisory by design; scores are not thresholds ([GOVERNANCE.md](../../GOVERNANCE.md#maintainer-continuity)) | [mutation-testing.yaml](../../.github/workflows/mutation-testing.yaml) [EV-615] |
| Dependabot version updates | Seven weekly update entries: both `uv` trees, both `npm` trees, GitHub Actions and both Docker contexts | Monday schedule | Not a gate; its pull requests pass the gates above | [dependabot.yml](../../.github/dependabot.yml) [EV-616] |
| Release required-checks verification | The image commit has successful checks for every context the `main` ruleset requires | `build-push.yaml`, before any build | Blocks image publication (§ 6.2) | `scripts/cicd/check_release_required_checks.py` [EV-625] |

### 2.3 `elspeth-lints` rule families

`elspeth-lints` is the project's own static analyser
([ADR-023](../architecture/adr/023-custom-python-ci-analyzer.md)); every
ELSPETH-specific CI invariant must be a rule in it, which the rule
`meta.no-new-bespoke-cicd-enforcer` enforces
([ADR-043](../architecture/adr/043-project-tooling.md)). The live registry at
`49c184508` holds **24 rules in 10 families** (instrument: load
`DEFAULT_REGISTRY` from `elspeth_lints.core.registry`, call
`DEFAULT_REGISTRY.load_builtin_rules()`, list `ids()`; the imported module
resolved to this checkout) [EV-608].

| Family | Rules | What it guards | Where it runs |
|---|---|---|---|
| `audit_evidence` | 4 | Audit-record classes use the nominal base; exception classes declare their trust tier; post-init validation has matching read-side guards; graph errors name the component | CI; pre-commit on trigger paths |
| `composer` | 2 | Composer error handlers are ordered correctly; tool handlers raise the argument-error type the LLM loop expects | CI; pre-commit (changed web files and policy paths) |
| `contract_invariants` | 4 | Adapter method budget; portable SQL inserts; session engines built through the one factory; validation functions do not return success from skipped branches | CI; pre-commit |
| `immutability` | 2 | Frozen dataclasses freeze container fields recursively and annotate them as immutable | CI; pre-commit on trigger paths |
| `manifest` | 3 | Contract-site manifest, stale-symbol inventory, obsolete test assertions | CI (`contract_manifest`, `symbol_inventory`); tests |
| `masquerade` | 1 | Dynamic attribute probes (`getattr`, `hasattr`) outside recognised external boundaries | Whole-tree test `tests/unit/elspeth_lints/test_masquerade_gate.py` in CI `Test` |
| `meta` | 1 | No new bespoke CI enforcers outside `elspeth-lints` | CI; pre-commit |
| `plugin_contract` | 3 | Plugin component types, option metadata, declared version and source-file hash | CI; pre-commit on trigger paths |
| `trust_boundary` | 3 | Each `@trust_boundary` names a real parameter, is Tier 3, and cites a real test that asserts its raising behaviour | CI only |
| `trust_tier` | 1 | `tier_model`: defensive patterns and upward imports (§ 2.4) | Pre-commit ratchet; operator verification |

The gates are built to fail rather than pass when they examine nothing:
`elspeth-lints check` exits 2 without an explicit `--rules` selection, and
CI passes `--fail-on-inert` so a rule that matches no files fails. The
masquerade test starts with a self-test that a fresh banned site must fire
[EV-609].

### 2.4 Trust-tier model and signed suppressions

The `trust_tier.tier_model` rule detects coding patterns that hide failures
at trust boundaries, such as defaulted lookups and broad exception handling
on data ELSPETH owns, and imports that break the layer model. Approved
exceptions live in an allowlist under `config/cicd/enforce_tier_model/`
[EV-610].

- Each judged exception carries a verdict from an LLM judge and an HMAC
  signature. The signing key is held by the operator only, never by CI or an
  AI agent ([custody rule \[O1\]](../judge-signature-handoff.md#the-custody-rule-o1),
  [07 § 5.1](07-secrets-and-key-management.md#51-judge-metadata-hmac-key)).
- Agents stage an unsigned review bundle through the key-free `elspeth-judge`
  tool server. The operator's keyed `sign-bundle` step re-derives every
  binding from the tree, re-runs the judge for changed content, and aborts on
  any staleness before it writes.
- The judge runs with read-only access to the tree so it can inspect code
  before ruling ([toolchain](../maintainer/toolchain.md)).
- CI never signs; a test fails if a signing verb appears in the additional
  judge lint and quality workflow
  (`tests/unit/elspeth_lints/test_meta_ci_never_signs.py`).
- The `elspeth-lints-trust-tier` pre-commit hook compares the finding set of
  the working tree with that of `HEAD` and fails if the commit adds a
  finding. Contributors must show that a change adds no finding
  ([CONTRIBUTING.md § Gate: trust-tier lint corpus](../../CONTRIBUTING.md#gate-trust-tier-lint-corpus))
  [EV-610].

Any accepted exception risk is recorded in the
[risk register](15-risk-register.md).

### 2.5 Tests that pin the gates

Several tests check the workflows themselves, so a gate cannot be weakened by
editing YAML without a test failing [EV-617]:

| Test | What it pins |
|---|---|
| `tests/unit/test_ci_workflow_xdist.py` | Concurrency policy shared by the three push workflows; additional judge-workflow check name; CodeQL suites not filtered by severity; integration job fails closed; no workflow references the operator HMAC key |
| `tests/unit/test_build_push_release_checks.py` | Required-check verification before build; OCI revision label bound to the image commit; tags promoted only after smoke tests; Dockerfile inputs and runtime contract |
| `tests/unit/cicd/test_gateway_supply_chain.py` | Gateway lock freshness and a stale-metadata negative control; exact dependency-audit exceptions; CI aggregation; image qualification; exact-digest gateway publication, evidence and promotion contracts |
| `tests/unit/elspeth_lints/test_meta_ci_never_signs.py` | CI never signs judge metadata |
| `tests/unit/cicd/` | Trust-tier ratchet, state-engine CI selection, live-provider workflow |
| `tests/unit/web/composer/test_label_gate_direction.py` | The four label and direction combinations of the redaction gate, through the real scripts |

## 3. Test estate

### 3.1 Python tests by tier

Measured at `49c184508`. File counts filter `git ls-files tests/<tier>` for
`test_*.py`, which includes files directly under each tier as well as nested
files; the unit result was controlled against
`find tests/unit -name 'test_*.py'` (1,700 both ways). Item counts come from
`pytest tests/ --collect-only -q -n 0` (exit 0), counting collected node ids
by top-level directory [EV-618].

| Tier | Directory | Test files | Items in default selection |
|---|---|---|---|
| Unit | `tests/unit` | 1,700 | 53,991 |
| Integration | `tests/integration` | 238 | 3,399 |
| Property-based (Hypothesis) | `tests/property` | 74 | 1,154 |
| Invariants | `tests/invariants` | 20 | 676 |
| End-to-end | `tests/e2e` | 40 | 223 |
| Fixture self-tests | `tests/fixtures` | 2 | 40 |
| Gateway runtime | `tests/gateway_runtime` | 1 | 3 |
| PostgreSQL testcontainer | `tests/testcontainer` | 87 | 0 (deselected) |
| Performance | `tests/performance` | 18 | 0 (deselected) |
| **Total** | | **2,180** | **59,486** of 60,280 collected |

Hypothesis is imported by 112 files under `tests/`, 75 of them under
`tests/property` (`git grep -l 'from hypothesis\|import hypothesis'`).

### 3.2 What the default selection leaves out

`pyproject.toml` sets `-m "not slow and not stress and not performance and
not testcontainer and not live_provider"` and `-n 12`. **The default
selection excludes the PostgreSQL testcontainer tests**, which run only in the
separate CI job (§ 2.1) or with `pytest tests/ -m testcontainer -n 0`.

| Marker | Tests (whole tree) | Where they run |
|---|---|---|
| `testcontainer` | 652 | CI `Testcontainer` job, required |
| `live_provider` | 56 | Operator-gated (`--run-live-provider`) and the manual `state-engine-live-provider.yml` workflow; no automatic CI job |
| `performance` | 51 | On demand |
| `slow` | 37 | On demand |
| `stress` | 30 | On demand (needs the ChaosLLM server) |

Counts are from `pytest tests/ --collect-only -q -n 0 -m <marker>`. Markers
overlap; 794 distinct tests are deselected in total. `--strict-markers` and
`--strict-config` are set, so an unknown marker or setting fails collection
[EV-619].

### 3.3 Frontend tests

| Kind | Count | Where it runs |
|---|---|---|
| vitest test files (`src/elspeth/web/frontend/src/**/*.test.ts(x)`) | 238 (tracked; same count from `find`) | CI `Frontend unit` [EV-620] |
| Playwright spec files (`src/elspeth/web/frontend/tests/e2e/*.spec.ts`) | 25 | CI `Frontend E2E` (default configuration) [EV-620] |

### 3.4 Test discipline

[AGENTS.md § Test verification policy](../../AGENTS.md#test-verification-policy)
requires a suite's result to be read from its exit code, not from piped or
filtered output, and a claim such as "suite green" only after the process has
exited. `scripts/full-suite-gate.sh` runs the pre-merge stages as CI runs
them, records each stage's exit code, and hashes the tree before and after
so a run over a moving tree is not treated as evidence. `AGENTS.md` also
requires every ad-hoc measuring instrument to be run against a known positive
and a known negative before its result is trusted [EV-622].

## 4. Security review

### 4.1 What counts as security-relevant

[GOVERNANCE.md § Security governance](../../GOVERNANCE.md#security-governance)
lists authentication, authorisation, audit integrity, redaction, secret
resolution, CI/CD gates, dependency management, container artefacts and
assurance documentation. Changes to the composer's redaction policy carry an
extra, blocking label gate (§ 2.2). Composer changes are also bound by two
invariants that no latency or cost argument overrides
([AGENTS.md § Composer invariants](../../AGENTS.md#composer-invariants-non-negotiable))
[EV-603] [EV-613].

### 4.2 Review records

Review records are kept under [docs/reviews/](../reviews/): 36 review entry
points dated 2026-09-20 to 2026-09-28 at `49c184508` (35 top-level records
plus the release web-review README) [EV-623]. Those most relevant to security:

| Date | Review | Method |
|---|---|---|
| 2026-09-23 | [Pre-publication security plan review](../reviews/2026-09-23-pre-publication-security-review.md) | Four independent readers (source reality, architecture, test quality, downstream effects); read-only GitHub secret and ruleset inventory |
| 2026-09-23 | [Web tier review, 48-hour window](../reviews/2026-09-23-release-0.8.1-web-review/README.md) | Diff review of 133 commits in 39 bundles and 9 seams; every finding sent to an independent refuter, high findings to a second |
| 2026-09-23 | [Identity / SSO programme completion adjudication](../reviews/2026-09-23-identity-sso-completion.md) | Completion check of the identity programme |
| 2026-09-23 | [Single-developer assumptions audit](../reviews/2026-09-23-single-developer-assumptions.md) and [in agent tooling](../reviews/2026-09-23-single-developer-tooling-assumptions.md) | Where the project depends on one person |
| 2026-09-27 | [Composer provider failure boundary gap analysis](../reviews/2026-09-27-composer-provider-boundary-gap.md) | Boundary analysis |
| 2026-09-28 | [Gateway boundary implementation review](../reviews/2026-09-28-gateway-boundaries-implementation-review.md) | Implementation review |

Open or accepted findings from these reviews belong in the controlled
[risk register](15-risk-register.md), not in this public process document.

### 4.3 Adversarial review tooling

The tracked charter, trigger and reverted-guard detector form the repository's
adversarial review tooling [EV-624].

- **Red-team agent.** `.claude/agents/red-team.md` (tracked) is an agent
  charter whose task is to disprove that a change works: tests that pass for
  the wrong reason, fixes reverted while their tests survive, path and
  normalisation escapes, exit-code conflation, gates that fail open, and
  surviving mutants ([scripts/red_team/README.md](../../scripts/red_team/README.md)).
- **Trigger.** `scripts/red_team/trigger.py` maps a commit's changed paths
  onto security seams (authentication, secrets, security, policy gates, state
  machines, CI/CD) and launches two or three reviewers with different attack
  angles. An opt-in post-commit hook runs it.
- **Reverted-guard detector.** `scripts/red_team/meta_check.py` finds recent
  commits whose added production lines have gone while their added test lines
  remain — the sign of a silently reverted fix.
- Findings are kept locally for the maintainer to triage; automated review
  never publishes issues.

### 4.4 Deployment record — independent review

| Item | Value |
|---|---|
| Independent (non-maintainer) code or design review performed for this release, by whom, scope | DEPLOYMENT-TODO: |
| Penetration test ([14](14-security-testing.md)) | DEPLOYMENT-TODO: |
| Review findings accepted as residual risk, and by whom ([15](15-risk-register.md)) | DEPLOYMENT-TODO: |

## 5. AI-generated code: how assurance compensates

ELSPETH's code is generated and reviewed by AI agents under one human
maintainer ([01 § 8](01-system-overview-and-boundary.md#8-maturity-and-assessment-constraints)).
The table maps failure modes typical of generated code to the controls that
address them. The controls reduce the need for, but do not replace,
independent human review and testing ([14](14-security-testing.md)).

| Failure mode | Controls |
|---|---|
| Code that looks right but is wrong | Strict mypy; 59,486 default tests plus 652 PostgreSQL tests; property-based tests; coverage floors; CodeQL (§ 2.2, § 3) [EV-612] [EV-618] [EV-619] [EV-621] |
| Defensive code that hides errors instead of surfacing them | Trust-tier model with judge-reviewed, operator-signed exceptions (§ 2.4); masquerade gate on attribute probes; `validation_theatre` rule; [ADR-032 validate by trust domain](../architecture/adr/032-validate-by-trust-domain.md) [EV-609] [EV-610] |
| A change that is green locally but breaks a whole-tree property | Whole-tree gates pin exact site sets, output bytes and plugin source hashes ([CONTRIBUTING.md](../../CONTRIBUTING.md#whole-tree-gates-and-conventions-you-will-hit)) |
| Tests that pass for the wrong reason | Red-team agent charter (§ 4.3); anti-inert gate design (§ 2.3); positive and negative controls required for every measurement (§ 3.4) [EV-609] [EV-622] [EV-624] |
| A fix silently reverted | Reverted-guard detector (§ 4.3) [EV-624] |
| A gate weakened or a suppression approved by the agent itself | Tests that pin workflows (§ 2.5); redaction direction label gate; signing key never held by CI or agents (§ 2.4) [EV-610] [EV-613] [EV-617] |
| Overclaimed results in reports or commits | Exit-code-only reporting rule; claims must show the measuring command's output ([AGENTS.md § Claims must be measured](../../AGENTS.md#claims-must-be-measured)) [EV-622] |
| The LLM path in the product being replaced by server-side shortcuts | Composer invariants (§ 4.1) |
| Unreviewed agent tooling changing behaviour | Adding or removing a tool with standing agent instructions is a recorded decision ([ADR-043](../architecture/adr/043-project-tooling.md), [ADR-046](../architecture/adr/046-audit-grade-is-a-product-characteristic.md)) |

Limits an assessor should weigh:

- There is no independent human review within single-maintainer mode
  (§ 1.2). Review depth depends on the gates, the lints and the review
  records [EV-603].
- Several reviews in § 4.2 were carried out by AI agents. Independence there
  means separate agent contexts, not separate organisations [EV-623].
- Accepted residual risks belong in the controlled
  [risk register](15-risk-register.md).

## 6. Build and release integrity

### 6.1 Build inputs

- **Base images pinned by digest** in the `Dockerfile`: the Node frontend
  builder (`node:24.18.0-bookworm-slim@sha256:…`), the Python builder
  (`python:3.13-slim@sha256:…`), the `uv` binary image and the distroless
  runtime (`gcr.io/distroless/python3-debian13:debug-nonroot@sha256:…`)
  [EV-627].
- **Locked dependencies:** the root `uv.lock`, the separate `gateway/uv.lock`
  and the frontend `package-lock.json` [EV-704] [EV-706].
- **Gateway build inputs pinned and frozen:** its Python 3.12 Alpine builder
  and runtime and its `uv` source are pinned by digest. Separate build and
  runtime environments consume `gateway/uv.lock` with `--frozen`; the exact
  local wheel is installed with `--no-deps` [EV-722].
- **A reviewed trust root** (the AWS RDS CA bundle) is baked into the image
  and checked against a committed SHA-256 at build and smoke time [EV-627].
- Base-image and dependency patching are covered in
  [10](10-vulnerability-and-supply-chain.md).

### 6.2 When an image is published

`build-push.yaml` runs on three triggers:

| Trigger | Condition |
|---|---|
| After CI on `main` | The CI run concluded `success`, was a `push` event, and came from this repository (not a fork) |
| Version tag `v*` | Any pushed tag; a hyphen marks a pre-release, published as a GitHub prerelease |
| Manual dispatch | Registry choice: GHCR, ACR or both |

The triggers and their admission conditions are defined by the release
workflow [EV-625] [EV-626].

In every case the first step,
`scripts/cicd/check_release_required_checks.py`, reads the live `main`
ruleset, takes its required status checks, and refuses to publish unless the
image commit has a successful check for every one. A missing, pending or
failed check stops the job [EV-625].

The workflow then performs the following sequence [EV-626]:

1. A lean PostgreSQL image is built and tested before any registry login.
2. The main multi-architecture image (`linux/amd64`, `linux/arm64`) is built
   and pushed as `sha-<commit>`, with the OCI `revision` label set to the
   commit. When GHCR is selected, the workflow also builds the separately
   named in-tree reference gateway for both platforms and binds it to the same
   source commit.
3. When both registries are selected, ACR receives a digest-preserving copy
   of the GHCR image, and the workflow asserts the two digests are equal.
4. Each main-image digest is signed (§ 6.3). The exact gateway digest is
   scanned per platform, signed and verified under the workflow identity.
5. The smoke-test job pulls the main image by digest and checks the non-root
   runtime identity, data directories, database drivers and trust root, runs
   the CLI, and runs example pipelines whose outputs it checks [EV-712]. When
   a gateway digest exists, the job also checks its source revision, runtime
   identity, UID/GID 65532, read-only execution and health.
6. For a tag, the release job promotes the qualified main-image digests and
   the exact GHCR gateway digest to their tag names, then creates the GitHub
   release [EV-626]. The ACR copy/build branches apply only to the main image;
   the reference gateway publication contract is GHCR-only.

### 6.3 Signing, provenance and SBOM

- **Signing.** Each pushed digest is signed with `cosign sign` in keyless
  mode: the workflow's GitHub OIDC token (`id-token: write`) obtains a
  short-lived Sigstore certificate that names the workflow and ref. This
  workflow uses no long-lived signing key [EV-626].
- **Provenance and SBOM.** The build sets `provenance: true` and
  `sbom: true`, so BuildKit attaches a provenance attestation and an SBOM
  attestation to the image index. SBOM content is covered in
  [10](10-vulnerability-and-supply-chain.md) [EV-626].
- **Reference gateway evidence.** The GHCR gateway build requests maximum
  provenance and an SPDX SBOM, retrieves both from the exact output digest,
  scans its amd64 and arm64 platform images with a zero High/Critical
  threshold, verifies its keyless signature, and retains those five evidence
  files for 90 days [EV-709] [EV-722]. These are source-enforced workflow
  contracts. A successful GitHub run remains required to prove that a
  particular gateway digest and its evidence exist in the registry.

### 6.4 How a deployer verifies an image

Deploy by digest, never by a mutable tag. Before deployment, verify the
signature with the exact workflow identity, as the
[AWS ECS deployment package](../../deploy/aws-ecs/terraform/README.md) does
[EV-628]:

```bash
cosign verify \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  --certificate-identity "https://github.com/dta-au/elspeth/.github/workflows/build-push.yaml@refs/tags/<tag>" \
  "ghcr.io/dta-au/elspeth@sha256:<digest>"
```

The separately named reference gateway uses the same issuer and workflow
identity policy:

```bash
cosign verify \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  --certificate-identity "https://github.com/dta-au/elspeth/.github/workflows/build-push.yaml@refs/tags/<tag>" \
  "ghcr.io/dta-au/elspeth-llm-gateway@sha256:<digest>"
```

For an image built after CI on `main`, the identity ends in
`@refs/heads/main`. Then confirm that the image's
`org.opencontainers.image.revision` label names the commit being assessed.
The Azure Container Apps runbooks carry an equivalent step
([first deployment](../runbooks/azure-container-apps-deployment.md),
[redeploy](../runbooks/azure-container-apps-existing-service-redeploy.md))
[EV-628].
A signature applies to the reference that was signed: copying an image to
another registry does not copy its signature unless that copy was signed too.

### 6.5 Deployment record — release acceptance

| Item | Value |
|---|---|
| Image digest deployed, and the registry it was pulled from | DEPLOYMENT-TODO: |
| Output of `cosign verify` with the exact certificate identity | DEPLOYMENT-TODO: |
| `build-push.yaml` run that produced the digest, and its required-check verification step | DEPLOYMENT-TODO: |
| OCI revision label matches the assessed commit | DEPLOYMENT-TODO: |
| Reference gateway exact GHCR digest and publication run | DEPLOYMENT-TODO: first successful GitHub publication run |
| Reference gateway SPDX SBOM and maximum-provenance files, bound to the source commit and digest | DEPLOYMENT-TODO: |
| Reference gateway amd64 and arm64 Trivy results at zero High/Critical | DEPLOYMENT-TODO: |
| Reference gateway Cosign verification output and workflow identity | DEPLOYMENT-TODO: |
| Reference gateway exact-digest read-only smoke result | DEPLOYMENT-TODO: |
| Who accepted the release for deployment, and when | DEPLOYMENT-TODO: |
| Gap between the deployed commit and the commit this pack was reviewed at | DEPLOYMENT-TODO: |
