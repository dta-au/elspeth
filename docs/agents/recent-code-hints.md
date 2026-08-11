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
- Assignment targets are executable syntax: attribute receivers, subscript
  containers and indices (including slices), and target-side named
  expressions must be inventoried for ordinary/annotated assignments,
  `for`/`async for`, `with`/`async with`, and comprehensions. Preserve CPython
  order with the shared target walkers. For chained or destructured
  assignment, freeze RHS binding/source evidence once before the first target
  store; re-resolving the RHS after a target-side walrus creates paired false
  positives and false negatives.

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

### 6. New-plugin exact inventories (2026-08-09)

Adding ANY builtin plugin fires a fixed set of whole-tree exact pins. For a
new TRANSFORM the full list (all hit while landing
`aws_textract_inline_analysis`, d181ee569) is:

- `tests/unit/plugins/test_discovery.py` `EXPECTED_TRANSFORM_COUNT`;
- `tests/unit/plugins/test_catalog_reference_content.py` — total reference
  count, per-kind `Counter`, `EXPECTED_BUILTIN_IDENTITIES`, and (for a
  non-profiled plugin) the `DIRECT_CONFIG_REFERENCES` count;
- `tests/unit/plugins/transforms/test_external_catalogue_metadata.py` — an
  EXTERNAL_CALL/NON_DETERMINISTIC transform must appear in
  `EXPECTED_EXTERNAL_TAGS` (exact tuple), `_REQUIRED_GUIDANCE` (casefolded
  substrings of the usage strings), and, when it surfaces
  externally-controlled text, `_REMOTE_CONTENT_PRODUCERS`
  ("untrusted before llm" must appear in its guidance);
- `tests/unit/plugins/test_validation_path_agreement.py` — any config with a
  `@model_validator` needs a rejection case in `_TRANSFORM_REJECTION_CASES`;
- `tests/unit/web/catalog/test_service.py` serialized-summary total and the
  knob-schema golden `tests/golden/web/catalog/knob_schema/<kind>__<name>.json`
  (generate via `CatalogServiceImpl._schema_cache`);
- `config/cicd/contracts-whitelist.yaml` for `__init__:config` /
  `probe_config:return` `dict[str, Any]` params (pre-commit Check Contracts);
- `capability_tags` gate: tuple of 2–6 lowercase kebab tags — a 7th tag fails;
- `PluginAssistance` text is scanned for credential-shaped patterns:
  "…token: SDK…" trips `token\s*:` — phrase around it;
- an untrusted-content producer also joins
  `_UNTRUSTED_REMOTE_CONTENT_PRODUCER_PLUGINS`
  (`src/elspeth/web/interpretation_state.py`) — that set is FAIL-OPEN, an
  unlisted producer silently reads as trusted;
- pin `source_file_hash` LAST (ruff/format edits restale it), via
  `scripts/cicd/plugin_hash.py`.

Sources have the same shape (see 0ec120e2d for the blob_rows list: source
count/names, registry, catalog, golden, contracts whitelist).

## Recent conventions (prune when archived)

- **2026-08-11 — caller-owned DB transactions cannot publish inline-custody files directly**:
  the guided-full settlement must insert the originating message and blob row
  in one transaction to satisfy the composite lineage FK, but a DB rollback
  cannot roll back a canonical filesystem rename. Stage those bytes at the
  bounded `.{blob_id}.inline-custody-staged` sibling, return the publication
  to the transaction owner, and arbitrate the outcome from the committed row
  under the same-session custody lock. A transaction error has an ambiguous
  commit outcome: re-query and publish when the row won; remove the stage when
  no row exists, or when this attempt created it and rollback restored an exact
  pre-existing `pending` row. Startup likewise discards a stage beside an exact
  `pending` row because inline settlement commits only `ready`; retaining that
  non-authoritative stage makes the supported pending retry state unbootable.
  The writer's `..{blob_id}.inline-custody-staged.custody.tmp`
  is always disposable, never row authority: startup enumerates it and the
  durable stage only after taking the session lock, then deletes temps and
  reconciles stages. Reject symlink/non-regular candidates and validate a
  row's exact canonical storage path before moving anything. Nofollow-open and
  retain both the `blobs/` root and session directory descriptors across live
  staging/publication/cleanup and the whole startup pass: checking only
  session/final components still lets a `blobs -> outside` ancestor escape
  custody. On first use, fsync the resolved data directory after linking
  `blobs/`, then fsync `blobs/` after linking the session directory; fsyncing
  only the stage file and session directory does not make those new ancestor
  entries crash-durable. Reconciliation hashes every candidate incrementally with
  `_STREAM_CHUNK_BYTES` through a no-follow descriptor; `Path.read_bytes()`
  under the custody lock recreates the several-large-blobs worker-memory
  failure this protocol is meant to prevent.

- **2026-08-10 — a `DateTime(timezone=True)` column does NOT round-trip aware on
  SQLite**: the blobs write stamps `datetime.now(UTC)`, the column declares
  `timezone=True`, and `BlobRecord.created_at` still comes back with
  `tzinfo=None` through the SQLite dialect. So a `created_at.tzinfo is not None`
  assertion reads as obviously correct, raises on EVERY write under SQLite, and
  passes under PostgreSQL — an environment-dependent production break that a
  PostgreSQL-only test lane would never show you. Check `created_at` for shape
  (`type(x) is datetime`) unless you have proven awareness on the backend you
  actually run. `verify_finalized_pipeline_custody`
  (`web/composer/pipeline_custody.py`) documents the narrowing and
  `test_verify_accepts_a_naive_created_at` pins it against a well-meaning
  re-tightening. Found while extracting the check from an abandoned WIP branch
  (a5d7fc0e7): salvaged WIP is a hypothesis, not reviewed code — probe its
  assertions against a live round-trip before porting them. The same function
  arrived using a `getattr(record, field_name)` loop, which gate 1/2 above
  forbid outright; `BlobRecord` is an owned type, so direct attribute access
  was always the correct form.

- **2026-08-09 — composer edge/route contract (Lane W2, elspeth-67b44040ee)**:
  scalar routing fields are the runtime authority; SINK-targeting edges are
  their mirror and must agree; node-targeting on_success edges are advisory.
  One shared predicate — `edge_lowering_error` in `web/composer/state.py` —
  decides which (component kind, edge type, target kind) combinations are
  legal, for BOTH upsert_edge admission and Stage-1 `validate()`; its full
  matrix is pinned by `test_edge_route_reconciliation.py` — extend the matrix
  and its pin together. upsert_edge/remove_edge/upsert_node reconcile the
  mirror through `_apply_sink_edge_route` / `_clear_removed_sink_edge_route` /
  `_reconcile_node_sink_mirror_edges` (tools/transforms.py); do not hand-sync
  a route in a new tool. Two traps: (a) deterministic runtime-fatal routes are
  now Stage-1 ERRORS, not warnings — `quarantine_unknown_output`,
  `failsink_unknown_output`/`_self_reference`/`_ineligible_plugin`/`_chain`,
  `aggregation_on_error_unknown_sink`, `gate_route_target_unknown`,
  `gate_routes_empty`, gate fork-consistency — so a test fixture with
  `on_validation_failure="quarantine"` and no quarantine sink no longer
  validates green (this silently broke dozens of fixtures; declare the sink or
  use "discard"); (b) one sink-route slot carries ONE edge
  (`edge_route_conflict`) — a second edge id on the same (from_node,
  edge_type) sink route is rejected at upsert and red in Stage 1.

- **2026-08-09 — plugin config unions use nominal admission plus owned MRO evidence**:
  `declares_discriminated_config_variants()` derives whether an admitted
  `BaseSource`, `BaseTransform`, or `BaseSink` class declares
  `discriminated_variants()` anywhere in its live MRO. Consumers such as the
  options-metadata lint first admit the nominal Base* category, then use that
  non-cached evidence and call the declared method directly. Do not bring back
  `getattr`/`hasattr` capability probes,
  treat the runtime-checkable structural Protocol as an identity control, or
  hard-code the currently known LLM implementations.
- **2026-08-09 — re-check mutable exception facts and every composer completion at their exit gate**:
  nominal ownership of an exception does not make its class or instance
  attributes immutable. Operator-facing acceptance envelopes must clamp
  `error_code` again at projection time, requiring an exact `str` from the
  closed vocabulary. In the freeform Composer, the B-4D-3 budget-exhaustion
  bonus response is still a model completion: apply the shared per-turn tool
  cap before its no-tool/generic-budget branch, using the already-charged
  composition count. Raw `_call_llm` test-seam responses that fail tool-call
  identity admission still re-raise `AuditIntegrityError`, but their LLM audit
  row is `MALFORMED_RESPONSE`/`malformed_response`, never `SUCCESS`.
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
