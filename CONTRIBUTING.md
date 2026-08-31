# Contributing to ELSPETH

Thanks for your interest in contributing to ELSPETH! This document covers the basics for getting started.

## Development Setup

Install Python 3.12 or newer, `uv`, Node.js 24, and npm 11. The repository's
`.node-version`, `package.json`, and lockfiles are the toolchain authority.

```bash
git clone https://github.com/dta-au/elspeth.git && cd elspeth
uv sync --frozen --extra dev --extra azure
source .venv/bin/activate

# Azure Blob integration tests require the Azurite emulator
npm ci

# Web Composer changes also require the separately locked frontend tree
npm --prefix src/elspeth/web/frontend ci
```

Use `uv` for Python package management and `npm ci` for both locked JavaScript
trees. Do not use `pip` directly or refresh a lockfile as an incidental setup
step.

For the repository-specific Caddy/systemd development install, follow
[Caddy development install refresh](docs/runbooks/caddy-development-refresh.md).

## Running Quality Checks

All of these must pass before submitting changes:

```bash
# Tests
.venv/bin/python -m pytest tests/ -v

# Type checking
.venv/bin/python -m mypy src/

# Linting
.venv/bin/python -m ruff check src/

# Config contracts verification
.venv/bin/python -m scripts.check_contracts
```

The root suite is scoped to `testpaths = ["tests"]`, so it does **not** cover
`gateway/`. That package (`elspeth-llm-gateway`) is standalone, with its own
`pyproject.toml` and test suite; if you change it, run its tests from
`gateway/` as well.

## Code Standards

- **No defensive programming** against our own code. Access typed fields directly (`obj.field`), not defensively (`getattr(obj, "field", None)`). See [Data Trust and Error Handling](docs/guides/data-trust-and-error-handling.md) for the trust-boundary rationale.
- **Three-tier trust model.** Handle errors differently based on data origin: crash on audit data corruption, quarantine bad user data, validate external API responses at the boundary.
- **No legacy shims or backwards compatibility.** When changing something, delete the old code completely. No deprecation wrappers, no commented-out code, no compatibility layers.
- **Test through production code paths.** Integration tests must use `ExecutionGraph.from_plugin_instances()` and `instantiate_plugins_from_config()`, not manually constructed objects.

## Writing Tests

Use the centralized test factories — do not construct framework objects directly:

```python
# WRONG - couples test to constructor signature
from elspeth.contracts.plugin_context import PluginContext
ctx = PluginContext(recorder=recorder, run_id="run-1", node_id="node-1", ...)

# RIGHT - use factories
from tests.fixtures.landscape import make_recorder_with_run
from tests.fixtures.factories import make_context
recorder, run_id = make_recorder_with_run()
ctx = make_context(recorder=recorder, run_id=run_id)
```

**Key factories:**

| Factory | Location | Purpose |
|---------|----------|---------|
| `make_recorder_with_run()` | `tests.fixtures.landscape` | Create `LandscapeRecorder` with a registered run |
| `make_context()` | `tests.fixtures.factories` | Create `PluginContext` with correct wiring |
| `make_landscape_db()` | `tests.fixtures.landscape` | Create in-memory `LandscapeDB` |
| `register_test_node()` | `tests.fixtures.landscape` | Register a node in the audit trail |
| Shared plugins | `tests.fixtures.plugins` | `PassthroughTransform`, `FailingSink`, etc. |

**Additional quality checks:**

```bash
# Tier model enforcement (layer dependency detection)
env PYTHONPATH=elspeth-lints/src .venv/bin/python -m elspeth_lints.core.cli check --rules trust_tier.tier_model --root src/elspeth
```

## Whole-tree gates and conventions you will hit

This section is the durable, tool-neutral list of the checks and conventions
that turn a branch red for a contributor who does not know them. Each rule
below was learned from a real incident; the dated record of when and why —
commit hashes, ticket ids, measured numbers — is kept in
[docs/agents/recent-code-hints.md](docs/agents/recent-code-hints.md), which
links back to the headings here. Read this section before writing code; read
the appendix when you need the history behind a rule.

### Why a green scoped run proves nothing

A large share of the test suite asserts over the **entire tree with exact
expected sets**: the set of dynamic-attribute sites, the set of unadjudicated
`getattr` probes, the count of registered plugins, the bytes of a golden file,
the hash of a plugin's source. A change can be locally green, fully typed, and
lint-clean and still fail one of these for everyone on the branch, because the
gate that catches it lives in a test file you did not run.

- Run the full `pytest tests/` (the CI-equivalent selection) before you
  consider a commit done. At an absolute minimum run every gate listed below
  whose tree you touched.
- Several gates run **only in CI** (`trust_boundary.tests`, the plugin
  `source_file_hash` check): a green local suite and a green pre-commit hook
  prove nothing about them. Run the CI command yourself; the commands are
  given under each gate.
- Every whole-tree gate scans `.agents/skills/**/*.py` and `scripts/` as
  production code. Only `.claude/worktrees/` is excluded. A helper script
  under those paths obeys the same rules as `src/`.
- Whole-tree measurements are only evidence when the tree was frozen for the
  duration: on a shared checkout, record `git rev-parse HEAD` and a hash of
  the files you are measuring before and after the run, and discard any run
  where they moved. Prefer a worktree for long runs.
- "Zero findings" from a probe that has never been shown to find anything is
  a failure to look, not a result. Run a positive control alongside every
  negative measurement.

### Gate: attribute contracts (dynamic-attribute sites)

**Pins:** the exact set of `getattr` / `hasattr` / `inspect.getattr_static` /
`__getattr__` sites under `src/elspeth/web/sessions` and
`src/elspeth/web/composer`. Only the ADR-032 LiteLLM admission boundaries (the
`_admit_*` parsers and `_capture_composer_llm_completion_fields`) may use
dynamic attribute access there.

```bash
.venv/bin/python -m pytest tests/unit/web/test_sessions_composer_attribute_contracts.py
```

**Rule.** On a type ELSPETH defines, use direct attribute access; if the
attribute is optional, make it a real field with a default rather than
probing for it. Only when parsing an object ELSPETH does not own is a
sentinel `getattr` legitimate, and that is a Tier-3 admission boundary:
`getattr` with a sentinel, assert the value, construct an owned type, **and**
extend the gate's expected set deliberately in the same change.

**Do not** probe a plugin class with `getattr(cls, "input_schema", None)`:
`BaseTransform.__init_subclass__` moves the class-body declaration into
`cls._declared_input_schema`, so the probe returns a confident wrong answer
for every plugin. Read `_declared_input_schema` and run a positive control.

### Gate: masquerade sites (tests included)

**Pins:** every `getattr`, `hasattr` and `getattr_static` probe in the whole
repository — **tests, scripts and skills included** — against the
adjudicated baseline `config/cicd/masquerade_baseline.yaml`. A baseline entry
binds a sorted `probe_shapes` fingerprint for each occurrence, not only a
`(path, qualname, kind)` key and a count, so a one-for-one rewrite of a site
(literal field to reflection, a changed receiver or default, an imported alias
rebinding) fires `probe-shape-drift` even when the key and count are unchanged.

```bash
.venv/bin/python -m pytest tests/unit/elspeth_lints/test_masquerade_gate.py
# after a legitimate change to a probe site:
.venv/bin/python -m elspeth_lints.rules.masquerade.seed_baseline
.venv/bin/python -m elspeth_lints.rules.masquerade.seed_baseline --check   # exits 0 once the reseed is complete
```

**Re-pin legitimately.** Run the seeder and commit the regenerated baseline
in the same change. It preserves an existing classification and
justification only when key, count and shapes all still match, and resets
changed or new subjects to `unadjudicated`, which you then adjudicate with a
reason. Never hand-edit a fingerprint. Removing a baselined site is also a
gate edit: reseed.

**Traps that have fired.**

- Parametrizing a test by attribute *name* and resolving with
  `getattr(module, name)` is a probe. Parametrize with the objects and keep
  readable ids via `pytest.param(..., id="...")`.
- `getattr(obj, "x", None)` "to be safe" on an owned type is the defect the
  gate exists for: the default hides `AttributeError` and lets impostors
  pass. Use direct access. If a test fake then breaks, fix the **fake** to
  model the real contract; never loosen production code to tolerate a fake.
- Passing `getattr`/`hasattr` as a *value* (`wrap(getattr)`,
  `partial(getattr, o)`, `[getattr]`) is inventoried where the carrier is
  called. Aliasing a builtin is not an escape hatch.
- A platform-conditional stdlib constant read with
  `getattr(os, "O_NOFOLLOW", None)` is a masquerade entry and a tier-model
  finding; the honest form is a direct read under `except AttributeError`.
- Never build an unbounded dotted-target string in a loop (`node = node.next`):
  the resolver's loop-head fixpoint terminates only because targets are
  capped at eight segments.

**Do not rewrite the probe resolver.** `elspeth_lints/.../masquerade/inventory.py`
is the single authority; two full replacement attempts were built and rejected
by independent review. The standing stop rules are: no CFG, SSA, history,
replay, lazy-cache or object-emulator growth, and no more than 2.5x runtime
per input doubling. New semantic coverage lands as narrow, ticket-level red
tests against the existing visitor. Corpus agreement cannot validate a
resolver change — the evasion shapes are absent from a tree that passes — so
the oracle is adversarial hand-written cases. `probe_shape` is computed from
the AST node and kind and is invariant under resolver changes; land the digest
comparison as a test before touching resolution.

### Gate: trust-tier lint corpus

**Pins:** the finding corpus of the `elspeth-lints` static-analysis rules over
`src/elspeth`. The gate is deliberately fail-closed (exit 1 with a standing
corpus) until the operator signs the package; do not expect zero and do not
try to clear it during ordinary feature work.

```bash
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  .venv/bin/python -m elspeth_lints.core.cli check --rules all --root src/elspeth
```

**Obligation.** Capture the corpus before your change, capture it after,
and diff: you must add nothing. Compare finding *sets*, not counts, and
take the baseline after merging the branch you will land on, or sibling work
shows up as your delta. See
[Convention: lints and tier-model tooling](#convention-lints-and-tier-model-tooling)
for the allowlist and signature mechanics.

**Companion CI-only gates.** The boundary-declaration rules run only in CI:

```bash
.venv/bin/python -m elspeth_lints.core.cli check \
  --rules trust_boundary.tests,trust_boundary.scope,trust_boundary.tier --root src/elspeth
```

Run this after any `@trust_boundary` / `@observation_boundary` edit or any
edit to a test one of them references. No pre-commit hook covers it and the
runtime decorator stores `test_fingerprint` without checking it.

### Gate: wire-shape templates

**Pins:** every wrapped-diagnostic producer template in
`src/elspeth/web/composer/no_tool_policy.py`. Templates and
`_split_wrapped_diagnostic` derive from the single
`_wrapped_diagnostic_wire_shape` source and a round-trip test exercises each
one. The round-trip case list is hand-maintained, and
`test_the_round_trip_parametrization_covers_every_wrapped_template`
AST-scans the module and fails when a template has no case.

Rules for a new backend-authored suffix:

- Build it through `_wrapped_diagnostic_template`; never hand-assemble a
  separator, marker, header or footer.
- Add its round-trip case.
- Register it in `_canonical_trusted_suffix_segments` with a matching
  `_split_wrapped_diagnostic` arm. Registration in the `_AugmentationBranch`
  literal governs only the prefix invariant; without the segment registration
  `visible_message_segments` fails closed and publishes your operator notice
  as model prose, silently.
- Exactly one backend suffix per message. Two concatenated suffixes match no
  recognizer arm and demote both disclosures; rebuild from
  `raw_assistant_content` and fold the second fact into the first suffix.

Shared disclosure text (for example the advisor-cohort withheld-prose
disclosure) lives as one constant in that module and is appended to every
message that needs it; never fork a per-message copy.

### Gate: declared oracles pin output bytes

**Pins:** content hashes, golden files and byte-exact corpora — the
branch-loss oracles, the DAG scenario corpus, the web catalog knob-schema
goldens under `tests/golden/`, the state-engine proof catalogs. A
behaviour-preserving refactor of a producer can still move pinned bytes.

Grep for hashes and golden files near what you touch, or run the full suite.
When a golden must move, regenerate it with the producer's own tool (each is
named under the gate that owns it) and review the diff: only the bytes your
change explains may move. A frozen oracle under
`tests/fixtures/dag_scenario_corpus/oracle_freeze/` moving means semantics
changed, which is a ruling, not a re-pin. A new corpus case fails the frozen
oracle closed; the fix is a scoped write of that case, never a full
regenerate.

### Gate: plugin inventories, source hashes, scenario-corpus manifest, fingerprint baseline

**Pins:** the exact inventory of builtin plugins and, per plugin, the bytes of
its source. Adding or editing any plugin under `src/elspeth/plugins/` fires
several of these at once, and most have no symptom in the plugin's own suite.

**Source hash.** Every plugin declares `source_file_hash`; the node audit
record carries it, and `docs/architecture/dag/scenario-corpus/v1/manifest.yaml`
pins the audit records literally. Any content edit — even adding a class
attribute — moves it.

```bash
# recompute AFTER `ruff format`; the hash line self-normalises
.venv/bin/python -c "from pathlib import Path; from scripts.cicd.plugin_hash import compute_source_file_hash as h; print(h(Path('src/elspeth/plugins/<kind>/<name>.py')))"
.venv/bin/python -m pytest tests/integration/core/dag tests/unit/architecture
```

Compare strictly (`Cls.source_file_hash == compute_source_file_hash(path)`);
a substring test passes on a comment. `plugin_version` is a static
declaration — do not bump it for a behaviour change. Re-pin in dependency
order: the manifest's literal hash bytes (they appear both plain and
JSON-escaped, so grep the bare hex token), then any
`resumed_full_projection_sha256`, then
`tests/unit/architecture/test_dag_scenario_corpus_contract.py::EXPECTED_CASE_REGISTRY_SHA256`.

**New plugin checklist** (every item is an exact pin):

- `tests/unit/plugins/test_discovery.py` — `EXPECTED_TRANSFORM_COUNT` (and
  the source/sink counterparts).
- `tests/unit/plugins/test_catalog_reference_content.py` — total reference
  count, per-kind `Counter`, `EXPECTED_BUILTIN_IDENTITIES`,
  `DIRECT_CONFIG_REFERENCES`.
- `tests/unit/plugins/transforms/test_external_catalogue_metadata.py` — an
  external-call or non-deterministic transform must appear in
  `EXPECTED_EXTERNAL_TAGS`, `_REQUIRED_GUIDANCE`, and, when it surfaces
  externally controlled text, declare
  `content_trust = ContentTrust.UNTRUSTED`; the catalogue guidance test and
  Composer prompt-shield admission both derive their producer vocabulary from
  that closed declaration.
- `tests/unit/plugins/test_validation_path_agreement.py` — any config with a
  `@model_validator` needs a rejection case in `_TRANSFORM_REJECTION_CASES`.
- `tests/invariants/test_input_schema_config_is_captured.py`
  (`_EXPECTED_MUTATION_REJECTIONS`) and
  `tests/invariants/test_transform_input_contract_is_satisfiable.py`
  (`_EXPECTED_ARMING_REJECTIONS`) — both subset-asserted against the live
  registry; an unlisted rejection hard-fails.
- `tests/unit/web/catalog/test_service.py` — the serialized-summary total
  (a bare `sum(...) == N`) and the knob-schema golden
  `tests/golden/web/catalog/knob_schema/<kind>__<name>.json` (generate via
  `CatalogServiceImpl._schema_cache`).
- `config/cicd/contracts-whitelist.yaml` — entries in both the
  `probe_config:return` block and the constructor block; the constructor
  entry's trailing segment must match the actual `__init__` parameter name.
- `capability_tags`: a tuple of 2–6 lowercase kebab-case tags.
- `PluginAssistance` text is scanned for credential-shaped patterns
  (`token\s*:` trips it); phrase around them.
- The state-engine lifecycle matrix: `scripts/state_engine_plugin_matrix.py`
  (`EXPECTED_COUNTS`, `EXPECTED_VARIANT_COUNT`),
  `tests/golden/state_engine/plugin_lifecycle_matrix.json`,
  `tests/unit/plugins/test_state_engine_plugin_matrix.py`, and the PB-09
  cases in **both** `docs/architecture/state_engine/proof-catalog/v2` and
  `v3` (`catalog.json`, `evidence_selectors.json`). The five matrix fields
  are derived field-by-field from the existing corpus, never guessed and
  never clustered; the inventory edit is a mirror sweep across both
  catalogs (surgical text edits — a `json.dumps` round-trip reflows v2),
  then `CANONICAL_V2_LEGS_SHA256`, `CANONICAL_V2_EXECUTION_PROFILES_SHA256`
  (`scripts/state_engine_assessment_lib/common.py`), the case-count pin, and
  `tests/unit/architecture/test_state_engine_catalog_contract.py::V2_CATALOG_SHA256`
  **last**.
- `src/elspeth/web/audit_readiness/boundary_expectations.py`
  (`EXPECTED_TRANSFORM_DETERMINISMS`); editing it requires the commit trailer
  `telemetry-backfill: audit-readiness`.
- The Python/TypeScript acronym mirror has no parity test: update both
  `src/elspeth/web/composer/guided/_display.py::_ACRONYMS` and
  `src/elspeth/web/frontend/src/components/catalog/pluginDisplayName.ts::ACRONYMS`.
- Pin `source_file_hash` last.

**Fingerprint baseline.** `@trust_boundary` decorators carry a
`test_fingerprint`, the SHA-256 of the AST dump of the test that
`test_ref` names. Editing that test's statements rotates it (comments,
whitespace and `ruff format` do not). Never hand-compute it: write the
decorator without one and paste the value the `trust_boundary.tests` rule
reports. Pinning a widened `suppresses=` is only honest when the named test
asserts the widened arm.

**Plugin discovery inside a worktree reads the main checkout** unless
`PYTHONPATH` points at the worktree (see
[Convention: repository and process hygiene](#convention-repository-and-process-hygiene));
a count that transiently matches a sibling branch's plugin set was never real.

### Gate: runtime-rejection parity

**Pins:** every `raise` under `src/elspeth/core/dag/` and
`src/elspeth/core/config.py`, plus every declarative pydantic `Field(...)`
constraint on those settings models, against a reviewed disposition in
`config/cicd/runtime_rejection_parity.yaml` (`mirrored` with a Stage-1
`error_code` or `fn:<validator>` counterpart, `abstains`, `structural`,
`not_authorable`, or `unmirrored` under a ratchet ceiling).

```bash
.venv/bin/python -m pytest tests/unit/scripts/cicd/test_runtime_rejection_parity_gate.py
.venv/bin/python scripts/cicd/runtime_rejection_parity.py --write   # seeds the new entry; adjudicate it
```

Adding a runtime rule without an authoring-time counterpart is exactly what
the gate exists to catch. Rewording a message re-keys the site; carry the
adjudication across. Never hand-edit a `key`. Stage 1 mirrors the runtime
name and label rules by calling the runtime's own validators, so test fixture
node ids and labels must be runtime-valid — fix the fixture, never relax the
mirror.

### Gate: CSS barrel structure

**Pins:** every custom property referenced with `var()` in the frontend is
defined in a stylesheet — an inline React style does not satisfy the
whole-tree token gate — and the `@media (forced-colors: active)` corpus in
`src/elspeth/web/frontend/src/styles/themes.css` is a single canonical final
block. Do not add a standalone forced-colors block before it: the contrast
gate treats the first block as canonical and inspects only that partial
corpus. Run the frontend suite (`npm --prefix src/elspeth/web/frontend test`)
after any stylesheet change.

### Gate: Playwright auth state

Playwright's global setup rewrites the shared `tests/e2e/.auth/user.json`
for the whole worktree. Distinct backend and frontend ports do not isolate
it, so two concurrent Playwright runs in one worktree corrupt each other's
authenticated state. Run every Playwright suite sequentially per worktree.

### Gate walker and scratch directory

**Pins:** that every whole-tree gate enumerates files through one authority.
`tests/unit/elspeth_lints/test_python_file_walker_authority.py` fails on any
literal `rglob("*.py")` or `os.walk(` under `tests/` — in docstrings and
comments too.

- Enumerate with `iter_gate_files(root)` / `iter_gate_sources(root)` from
  `tests/helpers/tree_gate.py`. They derive from the lints exclusion authority
  (`elspeth_lints.core.ast_walker.iter_python_files`), subtract everything git
  ignores so a local gate measures exactly the tree CI measures, and raise a
  named `TreeNotFrozenError` (a file vanished mid-walk: the run is void) or
  `GateSourceError` (unreadable or unparseable) instead of skipping. A walk
  that is genuinely not a Python-file walk goes in that test's
  `_NOT_PYTHON_FILE_WALKS` allowlist with a reason.
- Scratch reproduction tests live in `tests/_scratch/` and nowhere else. It is
  gitignored, pytest still collects it, and no gate can see it. A scratch file
  anywhere else under `tests/` is a gate input from the moment it exists, and
  deleting it during another run crashes that run.
- `.pre-commit-config.yaml`'s lint hooks are pinned by
  `tests/unit/elspeth_lints/test_pre_commit_triggers.py`; policy gates pass
  `--fail-on-inert`, changed-file hooks must not.

### Convention: trust-tier rules

The three-tier model in
[Data Trust and Error Handling](docs/guides/data-trust-and-error-handling.md)
is enforced by the `trust_tier.*` lint rules. The rules a contributor meets
most:

- **Tier 1 (our own audit data and owned types):** access directly and crash
  on violation. No `getattr`/`hasattr` defaults, no broad `except` that
  swallows, no defensive `isinstance` on a type we construct ourselves. A
  Tier-1 nominal invariant must raise; do not soften it to a warning.
- **Tier 2 (user data):** quarantine the row, never the run. A transform that
  cannot produce a row returns `TransformResult.error(...)` so the row leaves
  through `on_error`; a caught exception's tier is the discriminator, and the
  cheapest way to say so is a narrowed parameter type.
- **Tier 3 (external input):** parse at one declared boundary and construct an
  owned type. Read foreign data through a single named accessor per module
  (for example `_node_str_option(node, key)` in `interpretation_state.py`,
  `finish_reason_from_raw_response` for SDK responses) rather than scattered
  `.get()` reads.

**Declaring a boundary.** `@trust_boundary(source_param=..., suppresses=(...),
invariant=..., test_ref=..., test_fingerprint=...)` marks a Tier-3 parser;
`@observation_boundary` is the form for a pure projector that returns a
sentinel instead of raising and takes no test reference. What the lint's
suppression walk actually follows, measured:

- A boundary can only ever suppress **R1 and R5** (`contracts/trust_boundary.py`
  declares `BoundaryRule = Literal["R1", "R5"]`). R2 (`getattr` default), R4
  (broad handler), R6 (silent handler), R7, R8 and R9 findings need a real
  code change or a written rationale.
- The trail is kept through subscripts, attributes, `.get()`, iteration,
  unpacking, walrus, branched assignment, comprehension generators, `list()`
  or `cast()` wrapping on the right-hand side of an assignment, and a helper's
  *return value* (`x = helper(param)`).
- The trail is lost for a name bound inside a `try:` body and read **after**
  the try (whether the handler raises, returns, or reassigns), for nested
  `def`/closure bodies, for `zip(...)`/`enumerate(...)` loop targets, and for
  `cast(T, derived[k]).get(...)` in receiver position. The house shape is one
  `try:` around the whole parse, `raise ValueError` inside it, and one
  `except` that converts to the domain error. Do not rewrite loops into index
  arithmetic or drop `strict=True` to regain suppression; rationalise instead.
- Every decorator argument must be a literal (a non-literal voids that
  decorator with `R_TB_NONLITERAL`), `@classmethod` must be outermost, and a
  `test_ref` must resolve to a test whose own body holds the `pytest.raises`,
  calls the decorated function through `source_param`, and names an exception
  the `invariant` prose also names — the rule reads exception names out of the
  prose with an `*Error|*Exception|*Warning` suffix regex.
- Decide suppressibility by running the rule: the non-failing
  `R_TB_SUPPRESSED` observation stream names every site a decorator covers.
  Do not reason from the decorator, and re-verify any "not a boundary
  because X" comment before honouring it.
- Narrowing a broad `except` converts an R4 into an R6 at the same line;
  only a `raise` (or an explicit routed error result) clears it. Recording a
  constructed error record into a validator accumulator is explicit;
  `errors.append(str(exc))` is not. `raise X from None` inside a handler is
  not context-free.

**Exact-type idiom.** The house form for an owned-type check is
`type(x) is C`, but it is not a drop-in for `isinstance`: it gives mypy no
negative-branch narrowing on a union (check `reveal_type` on the else arm), it
is unconditionally false for `Enum` and for any subclass a test double
introduces, and every nested mapping reached through a `deep_freeze`d options
bag or `ToolResult.data` is a `mappingproxy`, so `type(x) is dict` there is
always false. Polarity decides how bad that is: a check that gates a control
block fails open. Keep `isinstance` where the negative arm or a subclass
matters, and say why in the rationale.

**Never shape code around the lint.** An honest fix is always preferred to
minimising allowlist or signature churn; re-signing is the operator's lowest
priority. Do not add aliases, padding, reordering, or dead code to preserve a
fingerprint, and never hand-edit a `judge_metadata_signature` or a
`test_fingerprint`.

### Convention: validate by trust domain

[ADR-032](docs/architecture/adr/032-validate-by-trust-domain.md): nominally
type what ELSPETH owns, parse what it does not.

- For an owned type, `isinstance` against the concrete class we define (or the
  exact-type form above). Plugin config unions use nominal admission of the
  `Base*` category plus owned MRO evidence
  (`declares_discriminated_config_variants()`), not capability probes.
- For a foreign object, sentinel `getattr` plus value assertions, then
  construct an owned type at the boundary.
- Never use a `runtime_checkable` Protocol as a security or dispatch control.
  It is structural, so an impostor passes; widening it silently reclassifies
  every implementation tree-wide; and since Python 3.12 it rejects
  dynamic-attribute objects such as pydantic `extra="allow"` models. Engine
  dispatch is nominal.
- pydantic passes a nested mapping through by identity even under
  `strict=True`, so a `dict[str, Any]` request field is not proof that its
  inner values are exact dicts. Dispatch on a discriminated union's own tag
  where one exists.
- An exact key-set assertion (`if set(item) != expected: raise`) makes every
  later `.get()` on that mapping an unreachable-`None` read; use subscripts
  after it, and prefer `_exact_nested_mapping`-style helpers that check and
  return in one step.
- A "does this literal name a row key?" question is
  `normalize_field_name(x) == x`, never `x.isidentifier()`, and a config-time
  name comparison compares canonical row keys, not config literals.

### Convention: composer invariants

The two non-negotiable rules — **the LLM does the job; no composer path
bypasses the provider** and **there are no tutorial-special paths** — are
stated in [AGENTS.md](AGENTS.md#composer-invariants-non-negotiable) and
[ADR-031](docs/architecture/adr/031-tutorial-is-a-fixed-script-canary.md).
Working rules that follow from them:

- A new node kind or behaviour arm is a **parity sweep** across every
  `node_type` dispatch site: the binder, proposal projection and
  `validate_payload`, wire cardinality, the frontend union / decoder /
  renderers, and the teaching skills. Never a lane-scoped schema narrowing.
- The composer authors `coalesce`; `row_union` is never authorable, and a
  `row_union`-bound fork inside any bound region is a build-time rejection.
- Scalar routing fields are the runtime authority; sink-targeting edges are
  their mirror and must agree. One predicate, `edge_lowering_error` in
  `web/composer/state.py`, decides legality for both `upsert_edge` and
  Stage-1 `validate()`; extend its matrix and its pin
  (`test_edge_route_reconciliation.py`) together, and reconcile the mirror
  through the existing `_apply_sink_edge_route` /
  `_reconcile_node_sink_mirror_edges` helpers, never by hand in a new tool.
- "Validated" is reserved for a green Stage-2 preflight. A skipped ledger row
  is not a verdict: `_skipped_checks` emits every check downstream of a halted
  stage as `passed=False` with `outcome_code=CHECK_OUTCOME_SKIPPED_AFTER_FAILURE`,
  so discriminate on `outcome_code` (or dispatch through
  `execution/completion_gates.advisor_signoff_check_failed`) rather than
  reading `passed` alone.
- Every advisor-cohort terminal publication is attributed through
  `record_advisor_terminal_publication`; adding a publication branch adds its
  literal and its emit.

### Convention: passes_through_input presence discipline

`passes_through_input` and `forwards_input_fields` are **presence** promises:
the field survives the transform. They say nothing about its value; the value
promises are `preserves_input_values` (transform) and `observed_value_type`
(source), and a presence flag never implies a value promise.

- Every test fake that models a plugin protocol must carry all of these
  attributes truthfully. The graph builder reads them directly, so a missing
  one is an `AttributeError` at build (or a silent structural-conformance
  failure at an `isinstance` site). Add the attribute to the fake; never
  weaken the builder's read. Grep for fakes that assign the flags in
  `__init__` as well as in the class body.
- The field-collision gates are capability-keyed: they arm only when
  `can_overwrite_input_fields(passes_through_input=, forwards_input_fields=)`
  (`contracts/field_collision.py`) is true. A fake that declares colliding
  output fields without the presence flags disarms the gate and goes green
  for the wrong reason. The capability is instance-level on `field_mapper`
  and the explode transforms; the class attribute is not evidence.
- `declared_output_fields` is an honest guarantee claim, never narrowed to
  keep a gate asleep. A reductive transform's output `SchemaConfig` must not
  carry the authored input `fields`.
- A `*_field` option with a non-`None` default leaks its column name into
  `consumed_input_fields` on every arm. Default such options to `None`,
  expose `read_<opt>` for the effective spelling and `named_<opt>` for the
  arm-aware declaration, and feed the latter through `declared_input_fields`.
  A str-defaulted `*_field` option is a required input.
- Forwarding transforms declare the extras they forward; the extras firewall
  walk is separate from the presence walk.
- **Zero rows is a fail state.** A transform that yields no row returns
  `TransformResult.error(..., retryable=False)`. An empty *value* (`,,,`, an
  empty line) is a row and is emitted. `can_drop_rows = True` with
  `TransformResult.success_empty()` is the only legal zero-emission shape and
  exists for genuine filters and bound-group openers, not for an expander
  handed an empty container.

### Convention: test discipline (fakes, mocks, fixtures)

- Use the factories in `tests/fixtures/` (see [Writing Tests](#writing-tests))
  and test through production code paths.
- A fake models the real contract. When production code and a fake disagree,
  fix the fake. A fixture that hand-builds a producer's output must match the
  shape the producer actually emits (for example a `ValidationResult` with
  the skipped tail); a shape no producer emits lets every test agree with a
  projection that never fires. Where possible build the fixture by calling
  the producer.
- `tests/unit/test_mock_discipline_baseline.py` pins unspecced mocks
  tree-wide. Give every `Mock` a `spec`, and put a fake's attributes on the
  fake rather than relaxing production reads.
- Declaration tests pin *existence*, not truth: a test that asserts a finding
  set (`Counter`) turns red when a finding is honestly removed — update the
  pin, do not keep the finding.
- Fixture node ids and labels must be runtime-valid (leading letter, at most
  38 characters, not `fork`/`continue`/`on_success`, sinks lowercase); a
  fixture with `on_validation_failure="quarantine"` must declare the
  quarantine sink.
- The `tests/unit/docs` suite pins wording in public documents (SECURITY,
  CONTRIBUTING, README); check it after a docs edit.

### Convention: audit and lineage recording

- Trust tier belongs to the row and is set at the source; downstream code
  reads it, never re-derives it.
- Batched rows are first-class ([ADR-020](docs/architecture/adr/020-retire-batch-llm-transforms.md)):
  every row leaves in good order or quarantines, and aggregation members'
  buffered acceptances keep the original `batch_id` across crash-retry.
- Branch-loss reasons are categorical tokens from the shared vocabulary of
  `record_coalesce_branch_loss`; group-settlement reasons are a closed
  `StrEnum` ([ADR-042](docs/architecture/adr/042-group-settlement-observability.md)),
  and merged-versus-failed is discriminated by release status, not
  completion. A new producer reuses the vocabulary rather than inventing
  prose.
- The loss seam is not single: coalesce branch loss, empty-group records
  (`record_empty_expansion`, gated on `creates_tokens`) and contract
  violations are separate ledgers with separate consumers.
- LLM messages are `Sequence[ChatMessage]`; `wire_messages` and
  `audit_messages` are the only two exits, and image bytes never reach audit,
  tracing, logs or exception text. Value-source findings are response and
  log egress.

### Convention: web composer and frontend

- Secret wiring is deny-by-default. `WebSettings.secret_wiring_allowlist`
  authorizes only exact `(secret, component_type, plugin, option_key)` matches.
  The component vocabulary is `source|transform|sink`, with aggregation and
  collector nodes represented by `transform`. Preserve all three enforcement
  seams: `wire_secret_ref` checks before mutation;
  `validate_secret_evidence` rechecks every authored marker so patch, YAML, and
  other marker entry paths cannot bypass the policy; and `/execute` requires a
  state-bound, out-of-band 428 acknowledgement before run creation and fanout.
  LLM and composer-tool arguments never grant execution approval, while
  credentials lowered from server-authored operator profiles remain exempt.
- Adding a field to `SourceSpec`/`NodeSpec`/`OutputSpec` or a composer tool
  argument fires three pins: the `canonical-field-inventory` table in
  `src/elspeth/web/composer/skills/pipeline_capabilities.md`, the redaction
  snapshot (`scripts/cicd/bootstrap_redaction_snapshot.py --write`; only
  hashes may move, never `sensitive_path_count`, unless a sensitive path was
  intended), and the frontend's strict guided wire decoder
  (`frontend/src/api/guidedDecoder.ts`, `exactRecord` key lists) — a
  production decoder that rejects unenumerated keys at runtime. Serialise
  optional spec fields as omitted-when-`None` so persisted
  `composition_content_hash` values stay byte-identical.
- The CSS barrel rules are under
  [Gate: CSS barrel structure](#gate-css-barrel-structure).

### Convention: lints and tier-model tooling

- Run the gate with an explicit `--rules` selection and `--root src/elspeth`;
  a bare `check` exits 2, and a cwd root walks `.venv`. The
  `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing`
  prefix lets a contributor without the operator key run it; shape-only
  verification cannot detect forged judge metadata, so CI re-verifies with
  the key before a merge is authoritative.
- The `trust_tier.tier_model` allowlist under `config/cicd/enforce_tier_model/*.yaml`
  seals each judged suppression with an operator-held HMAC signature. A
  signed entry binds by `scope_fingerprint` of the enclosing function, not by
  its `fp=` key or `ast_path` — drift in those two is normal after siblings
  add statements. Judge coverage by running the real allowlist and diffing
  against an allowlist-disabled run, never by comparing keys. Per-file
  `pattern:`/`max_hits:` ratchet blocks suppress without a ruling; driving a
  pattern to zero fails the gate with `Unused tier-model per-file rule`, so
  lower the ceiling in the same change.
- A keyless run reports a signed site as a finding anyway (verification is
  fail-closed without the key). If the right code change removes a finding at
  a signed site, make it; the entry becomes `stale_delete` and the operator
  re-signs once. Do not write a rationale that restates a still-binding
  ruling for code you did not change. Contributors never hold the signing
  key, never hand-edit a signature, and stage signing work through the
  `elspeth-judge` workflow described in AGENTS.md.
- `fp=` and `scope_fingerprint` are per enclosing scope: a docstring or
  string-literal edit in a sibling function does not stale another
  function's entry. A docstring added to the signed function shifts every
  `body[N]` index in it.
- The R5 exemption map `_R5_NAMED_BOUNDARY_CONTEXTS` is measured: every entry
  must resolve to exactly one live definition, and a moved function is not
  successor-included. `TierModelVisitor` is path-sensitive; computing a
  justify key by hand with the wrong relative path invents findings.
- Allowlist YAML enumeration is one non-recursive authority
  (`allowlist.iter_allowlist_yaml_paths`) that refuses nested documents; one
  stale entry makes the loader refuse the whole file, so a real-allowlist run
  can under-suppress.
- The pre-commit mypy hook typechecks a changed file's dependents, so a
  module others import through must re-export with the `X as X` form.

### Convention: repository and process hygiene

- The pre-commit secret scanner rescans every line of a touched file. Append
  `# secret-scan: allow-this-line` to a false positive; never bypass the hook
  with `--no-verify`. A failed hook leaves files staged.
- Worktrees symlink `.venv` to the main checkout. A bare `uv pip install`
  inside one clobbers the main venv, and a bare `python`/`pytest` inside one
  imports the **main** checkout's `elspeth`. Run
  `PYTHONPATH=<worktree>/src:<worktree>/elspeth-lints/src <venv>/bin/python -m pytest ...`
  and verify both `elspeth.__file__` and `elspeth_lints.__file__` before
  trusting a result; `elspeth_lints` lives in a separate source root.
- `git stash` is blocked by a hook; use worktrees or commits.
- `.claude/skills/**/*.py` is production code to every whole-tree test gate
  but is not under the `--root src/elspeth` tier gate; ruff `T20` is ignored
  there, as under `scripts/`.
- Keep this section current: when you land a new whole-tree gate or a new
  standing convention, add the rule here and the dated entry to
  `docs/agents/recent-code-hints.md` in the same commit.

## Commit Guidelines

- Keep commits focused on a single logical change.
- Write commit messages that explain *why*, not just *what*.
- Ensure all quality checks pass before committing.

## Reporting Issues

Open an issue on GitHub with:

- A clear description of the problem or suggestion.
- Steps to reproduce (for bugs).
- Expected vs. actual behavior.

For security-sensitive issues, follow [SECURITY.md](SECURITY.md) instead of
opening a public issue with details. Do not include exploit details, secrets, personal data, or sensitive audit material in a public issue.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
