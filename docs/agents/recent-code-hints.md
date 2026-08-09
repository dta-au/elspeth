# Recent code hints — READ BEFORE WRITING CODE

**Audience: agents. This is a rolling document.** It exists because agents keep
landing commits that pass their scoped test run and then break whole-tree
gates for every sibling on the branch (most recently 7201beeb7 →
elspeth-62a5aa4da8). Each entry is dated; when you land a new convention or a
new whole-tree trap, ADD IT HERE in the same commit. Prune entries once they
are covered by permanent docs or no longer bite. No sign-off ceremony — this
is a working document under the normal delivery posture.

## Whole-tree gates: a green scoped run proves NOTHING

These gates assert over the ENTIRE tree with exact expected sets. Your change
can be locally green, fully typed, and lint-clean, and still turn the branch
red for everyone. Run the full `pytest tests/` (CI-equivalent) before you
consider a commit done — or at absolute minimum run the gates below.

### 1. Attribute-contracts gate (2026-08-09)

`tests/unit/web/test_sessions_composer_attribute_contracts.py` pins the EXACT
set of `getattr`/`hasattr`/`getattr_static`/`__getattr__` sites in
`src/elspeth/web/sessions` and `src/elspeth/web/composer`. The contract:
**only ADR-032 LiteLLM admission boundaries may use `getattr`** (the
`_admit_*` parsers and `_capture_composer_llm_completion_fields`). Adding ANY dynamic attribute access
anywhere under those trees fails the gate repo-wide.

- Owned type (a class ELSPETH defines)? Use direct attribute access. If the
  attribute is optional, make it a real field with a default — do not probe.
- Genuinely parsing an object ELSPETH does not own? That is a Tier-3
  admission boundary: sentinel `getattr` + value asserts + construct an owned
  type, AND you must deliberately extend the gate's expected set. Do not do
  this casually.

### 2. Masquerade gate (2026-08-09)

`tests/unit/elspeth_lints/test_masquerade_gate.py::test_live_tree_has_zero_unbaselined_findings`
scans the WHOLE repo — **tests included** — for unadjudicated `getattr`
sites against `config/cicd/masquerade_baseline.yaml`. Traps that have fired:

- Parametrizing a test by attribute NAME and resolving with
  `getattr(module, name)` trips it. Parametrize with the objects directly and
  keep readable IDs via `pytest.param(..., id="...")` (see
  `tests/unit/web/composer/test_no_tool_policy_segments.py`).
- A `getattr(obj, "x", None)` "just to be safe" on an owned type trips it.
  The safe-looking default is the defect: it hides AttributeError and lets
  masqueraders pass. Rewrite to direct access; if a test fake breaks, fix the
  FAKE to model the real contract (give it the attribute), never the
  production code to tolerate the fake.
- Baseline entries bind a sorted `probe_shapes` fingerprint for every
  occurrence, not only `(path, qualname, kind)` and a count. A one-for-one
  rewrite (literal field to dynamic reflection, receiver/default change,
  imported alias rebinding) deliberately fires `probe-shape-drift` even when
  the key and count stay unchanged. Refresh with
  `python -m elspeth_lints.rules.masquerade.seed_baseline`; it preserves an
  existing classification/justification only when key, count, and shapes all
  still match, and resets changed or genuinely new subjects to
  `unadjudicated`. Do not hand-edit the fingerprints.
- Probe classification resolves `builtins.getattr` / `builtins.hasattr` and
  `inspect.getattr_static` through imports, lexical shadowing, reassignment,
  comprehensions, possible-target control-flow joins, and deferred module
  bindings. Abrupt-only paths do not pollute the reachable binding, but any
  reachable builtin target is still inventoried. Aliasing a builtin is not an
  escape hatch, and a rebound `@trust_boundary` source parameter no longer
  receives boundary amnesty.

### 3. Trust-tier lint corpus (standing)

`elspeth-lints check --rules all --root src/elspeth` is fail-closed (exit 1,
~3.1k-line corpus, tracked as elspeth-13f0cc04fb). Do NOT expect zero and do
NOT try to clear it. The obligation is: capture the corpus BEFORE your
change, capture it AFTER, and diff — you must add nothing. Never hand-edit a
`judge_metadata_signature`; never shape code to reduce signature churn.

### 4. Wire-shape templates (2026-08-08)

The wrapped-diagnostic producer templates and `_split_wrapped_diagnostic` in
`src/elspeth/web/composer/no_tool_policy.py` derive from ONE
`_wrapped_diagnostic_wire_shape` source, and a round-trip test pins every
template. Do not hand-assemble a SEPARATOR/MARKER/header/footer suffix; add
new templates through `_wrapped_diagnostic_template`.

Two corrections (2026-08-09, elspeth-2ed41f0a4a):

- The round-trip test's case list is HAND-MAINTAINED. Until now a template
  added without an entry was simply never exercised — the claim that it
  "fails" was false. `test_the_round_trip_parametrization_covers_every_`
  `wrapped_template` now AST-scans the module and fails when the list is
  incomplete, so add your entry when you add a template.
- Building the suffix through a template is only HALF the contract. A
  backend-authored suffix must ALSO be registered in
  `_canonical_trusted_suffix_segments` (with a matching
  `_split_wrapped_diagnostic` arm). Registration in the `_AugmentationBranch`
  literal governs the PREFIX invariant only, not the segment recognizer —
  miss it and `visible_message_segments` fails closed to one
  `AssistantTextSegment`, publishing your operator-facing notice as MODEL
  PROSE. It is silent: `enforce_augmentation_prefix_invariant` still passes.
- Corollary: exactly ONE backend suffix per message. Two concatenated
  canonical suffixes match no recognizer arm, so stacking a second
  announcement onto an already-augmented `ComposerResult.message` demotes
  BOTH disclosures. Rebuild from `raw_assistant_content` and fold the other
  fact into the single suffix's `Cause:` region instead.

### 5. Declared oracles pin OUTPUT bytes (standing)

Several suites pin content hashes, golden files, and byte-exact corpora
(e.g. the `*-lost-c` branch-loss oracles). A behavior-preserving refactor to
a producer can still change pinned bytes. Grep for hashes/golden files near
what you touch, or run the full suite.

## Recent conventions (prune when archived)

- **2026-08-09 — review bundles are v2 exact-source assertions**: staging and
  firing bind full Git HEAD, tracked-source dirty state, and every scanner
  Python/YAML byte. The YAML set is the production loader's non-recursive
  top-level `*.yaml` inventory (not nested YAML or `.yml`). Relevant untracked
  inputs (ignored included), harmless byte drift, or a HEAD advance invalidate
  the bundle even when its action list is unchanged. Scanner inputs must be
  non-symlink lexical Git paths: reject symlinks rather than resolving an alias to
  a tracked target, and strip ambient `GIT_*` variables from evidence commands.
  Transaction candidates supply physical allowlist bytes but retain the public
  allowlist path as their logical Git identity; never hash the candidate under
  its private transaction path.
- **2026-08-09 — `CompositionState._content_hash_memo`**: write-once memo
  slot read by `composition_content_hash` via DIRECT access. Every mutation
  constructor resets it in `__init__`. If you add a mutation path, reset the
  slot; if you build a `to_dict` stand-in for hashing tests, give it
  `_content_hash_memo: str | None = None`. Do not reintroduce `getattr` here
  (that was elspeth-62a5aa4da8).
- **2026-08-09 — advisor evidence has ONE derivation per surface**: in
  `web/composer/service.py`, anything rendered to the advisor must also be
  reachable by the deterministic injection pre-scan. Node control-flow fields
  now derive from `_advisor_control_flow_fields` — `_render_node_control_flow`
  publishes it and `_advisor_prompt_template_injection_finding` scans it. Add a
  new control-flow field THERE, never as a fresh `if node.x is not None` branch
  in the renderer; hand-enumerating the two consumers separately is what left
  `trigger` rendered-but-unscanned (elspeth-eacfec09a6). Two rules that look
  redundant but are not: the scan reads the COMPLETE value while the renderer
  truncates (scan broader than render, pinned by a disagreement test — do not
  "simplify" it to scan only what is rendered), and render-admission
  (`_advisor_summary_renders_option_value`) is a SEPARATE predicate from
  scan-shape (`_advisor_prose_shaped_option_value`) because the two consumers
  need opposite failure directions (elspeth-c1b8b26d32). Render paths that
  bypass the admission predicate entirely — e.g. `required_input_fields` via
  the `[requires: ...]` segment — need their own scan arm.
- **2026-08-09 — "validated" is reserved for a GREEN Stage-2 preflight**: the
  planner staging announce (`protocol.PIPELINE_STAGED_*`) is now FIVE
  constants, not two, selected in `service._stage_pipeline_plan` by the actual
  runtime-preflight verdict over `PipelinePlanResult.candidate_state`
  (elspeth-2ed41f0a4a). Only a green verdict may say "validated" or mint a
  `PipelineCommitIntent`. If you add a staging surface, pick a constant by
  verdict — do not reuse the green ones as generic staging copy.
  - The non-green arms split on SHAPE, not on `is_valid`. A red verdict and a
    pending-interpretation handoff are both `is_valid=False`, but only the
    first is a validator objection; reporting a pending review card as
    "issues that must be fixed" sends the operator hunting for a defect that
    is not there. Use `_is_pending_interpretation_handoff`, and note its
    blocker code is the lowercase `interpretation_review_pending` — import
    `INTERPRETATION_REVIEW_PENDING_CODE` rather than hand-writing the string,
    or your test fixture silently misses the arm it means to exercise.
  - Catch ONLY `ComposerRuntimePreflightError` around
    `_cached_runtime_preflight`. `RuntimePreflightCoordinator._capture` funnels
    every `Exception` — timeouts included — into that single envelope, so an
    `except TimeoutError` arm is dead code, and a test scripting a bare
    `TimeoutError` exercises a path production cannot produce.
    `asyncio.CancelledError` is a `BaseException`, escapes `_capture`, and must
    keep propagating: broadening the catch turns a cancelled request into a
    staged proposal carrying a verdict nobody waited for.
  - The non-green arms set `raw_assistant_content=""` (the replacement shape)
    because the `ComposerResult` field-pairing invariant requires it for any
    failed preflight; the green arm keeps `None` or it would falsely imply
    synthesis on a verbatim response.
- **2026-08-09 — registered `pipeline_decision` user_terms need THREE arms**:
  a new entry in `REGISTERED_PIPELINE_DECISION_USER_TERMS`
  (`web/interpretation_state.py`) is not usable until it also has (a) a
  binding arm in `validate_pipeline_decision_semantics` — a registered term
  that falls through validates on ANY node and wedges later at the hash — and
  (b) an arm in `pipeline_decision_artifact_hash` pinning exactly the material
  the review adjudicates. Gates bind on `node_type == "gate"`, NOT plugin
  (structural nodes have `plugin=None`). An exact-set test pins the closed
  registry; doc listings render `sorted(REGISTERED_...)` dynamically — never
  hardcode the set in prose. If the hash reads NodeSpec fields outside
  `options`, add a to_dict/from_dict round-trip test (`fork_to` is tuple in
  memory, list on the wire) so serializer changes cannot drift accepted
  reviews (elspeth-c2c35e52ae).
- **2026-08-09 — SQLAlchemy `Row`**: `.count` is the TUPLE METHOD, not a
  column. Access columns through `row._mapping` (elspeth-d5578ccd98 fallout,
  Lane B).
- **2026-08-08 — branch-loss reasons are categorical**: every
  `record_coalesce_branch_loss` producer emits bare tokens from the shared
  vocabulary; a new producer must reuse it, not invent prose reasons
  (elspeth-74b795208f).
- **2026-08-08 — forwarding transforms declare their extras**: the extras
  firewall walk is SEPARATE from the presence walk; a transform that forwards
  rows must declare the extras it forwards or downstream consumers see them
  truncated (elspeth-15c72686f2).
