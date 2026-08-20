# Recent code hints — READ BEFORE WRITING CODE

**Audience: agents. This is a rolling document.** It exists because agents keep
landing commits that pass their scoped test run and then break whole-tree
gates for every sibling on the branch (most recently 7201beeb7 →
elspeth-62a5aa4da8). Each entry is dated; when you land a new convention or a
new whole-tree trap, ADD IT HERE in the same commit. Prune entries once they
are covered by permanent docs or no longer bite. No sign-off ceremony — this
is a working document under the normal delivery posture.

- **2026-08-21 — a config-time name comparison must compare CANONICAL ROW KEYS,
  not config literals** (landed with elspeth-bb470636d1). The companion to the
  fixed-point entry below: that one fixed `field_mapper`'s GUARD, this one its
  config VALIDATOR, and either alone leaves the other's shape reachable.
  `_reject_overlapping_rename_graphs` rejected rename chains with
  `target in set(self.mapping)` — literal strings — while `process` deletes the
  renamed source by a key it picks at RUNTIME (the literal when it is already in
  the output dict, else `contract.resolve_name(source)`) and writes
  `output[target]` under the LITERAL target. So `{"a":"b","b":"c"}` was rejected
  while `{"a":"b","B":"c"}` was ACCEPTED and then destroyed the same value.
  The key is `_canonical_row_key`: `normalize_field_name`, except that a DOTTED
  name (a nested read, never a row key) and a name that normalizes to nothing
  (`ExternalHeaderError` — it names no field, so it can alias nothing) are keyed
  by themselves. Two traps:
  - The canonical key ADDS a rejection limb; it must NOT replace the literal
    one, and canonicalising the identity guard on its own is a REGRESSION that a
    green suite will not show you. A row can carry `'B'` and `'b'` as two
    DISTINCT keys — a source's `field_mapping` values bypass
    `normalize_field_name` (`resolve_field_names` validates them with
    `isidentifier()` alone) and headerless `columns` are taken as already-clean
    identifiers, so neither is ever lowercased. So "a row key is always a
    normalization fixed point" is FALSE, `{"B": "b"}` can be a real rename of a
    real distinct field, and a canonical identity guard waves
    `{"B": "b", "b": "y"}` through to destroy one value (measured: `{'B':'1',
    'b':'2'}` emits `{'y':'2'}`; the reversed spelling emits `{'y':'1'}`). Reject
    on EITHER limb. Conversely the canonical limb needs a CANONICAL identity
    guard, or `{"A": "a"}` — a verified no-op — is newly rejected at build time.
  - Pin the ACCEPTED shapes by running `process()`, not by asserting the config
    constructed. `assert transform._mapping == mapping` pins the EXISTENCE of a
    relief and says nothing about its safety; that gap is exactly what let the
    identity-guard regression above pass 125 green tests.
  - A "no false overlap" test only DISCRIMINATES when the invented key lands on
    the TARGET side, because membership is tested on targets.
    `{"meta.source": "a", "meta_source": "b"}` passes with OR without the
    dotted-name branch and proves nothing; `{"meta.source": "x", "y": "meta_source"}`
    is the shape that fails. Same for the unnormalizable branch: put the odd
    literal on the target (`{"!!!": "mapped", "a": "???"}`). Mutation-test every
    branch of the key function AND every limb of the rule that calls it — two
    branches survived the first mutation round here, and the identity-guard
    regression was caught only by an adversarial reviewer, not by any mutation
    of the code as written.
  The class is NARROWED, not closed: `resolve_name` is an `original_name` index
  lookup, not a call to `normalize_field_name`, so a source whose `field_mapping`
  moves a header off its normalized form (`{"Weird Header": "b"}`) still resolves
  to a key no config literal predicts, and no contract exists at construction to
  ask.

- **2026-08-21 — a caught exception's TIER is the discriminator, and the cheapest
  way to say so is a NARROWED PARAMETER TYPE** (landed with
  elspeth-181db83da7). Four traps:
  - `except SomePluginContractViolation` is the WRONG key for "may this be
    routed to `on_error`?". Registration in `TIER_1_ERRORS` is what forbids
    routing (ADR-008 §"TIER_1 registration is load-bearing"), and the registry
    cuts ACROSS the class tree in both directions: `SinkTransactionalInvariantError`
    is a REGISTERED `PluginContractViolation` (so it must keep crashing even
    though its base is Tier 2), while `UnexpectedEmptyEmissionViolation` is the
    one UNREGISTERED member of the otherwise-Tier-1 `DeclarationContractViolation`
    hierarchy. Always `except contract_errors.TIER_1_ERRORS: raise` FIRST — as a
    live module attribute, never a from-import, since the tuple is materialized
    per access (errors.py:1738).
  - The two violation hierarchies are SIBLINGS, not parent and child, and this
    is easy to get backwards from the names alone:
    `DeclarationContractViolation` (declaration_contracts.py) is
    `(AuditEvidenceBase, RuntimeError)` — `issubclass(DeclarationContractViolation,
    PluginContractViolation)` is FALSE. So a change scoped to one hierarchy does
    not reach the other, and `PassThroughContractViolation` /
    `DeclaredOutputFieldsViolation` / `UnexpectedEmptyEmissionViolation` are all
    outside a `PluginContractViolation`-keyed blast radius entirely.
  - Writing that check as `isinstance(exc, contract_errors.TIER_1_ERRORS)` is
    correct but EXPENSIVE: every `isinstance` in the tree is individually
    judge-gated by `trust_tier.tier_model`, and files carry per-file
    `max_hits` ceilings (`engine/executors/transform.py` is 2). One added
    check tips the ceiling and needs an operator signature. When the tier is
    statically known at the call site — and it usually is, because most sites
    construct the violation they raise — narrow the CALLEE's parameter
    annotation to the Tier-1 union instead and let mypy enforce it. Same rule,
    zero new suppressions, and the reader meets it where the decision is made.
  Third trap, the one that actually bit: a COMPENSATING record can outlive the
  defect it compensated for. `_record_terminal_contract_failure` pre-wrote a
  FAILURE/UNROUTED `token_outcomes` row because the run was about to abort
  (elspeth-82d4c5146c); once the violation became routable, the routing path
  wrote the real outcome and the pre-write became a duplicate the audit store
  rejects (`LandscapeRecordError ... IntegrityError`, raised from sink-effect
  finalization — nowhere near the code you changed). When you restore a path
  that was previously dead, grep for what was recording on its behalf. And
  verify BOTH destinations: a named `on_error` sink terminalizes through
  sink-effect finalization, `discard` through traversal, so a green run of one
  says nothing about the other. (An earlier revision of this entry claimed only
  the named-sink half raises on a duplicate. That was inferred, never measured,
  and it is FALSE: `ix_token_outcomes_terminal_unique`
  (`core/landscape/schema.py:677`) is keyed on `token_id` alone under
  `completed == 1`, with no sink discrimination, so either path raises on a
  double write. The duplicate direction is therefore self-detecting on both —
  it is the ZERO-write direction that has no automatic detection anywhere, and
  that is what you must check by hand.)

- **2026-08-21 — "does this config literal name a row key?" is
  `normalize_field_name(x) == x`, NEVER `x.isidentifier()`** (landed with
  elspeth-f262a8c678). Sources key a row by the NORMALIZED header, and
  `normalize_field_name` lowercases and keyword-suffixes as well as scrubbing
  punctuation. So `'B'`, `'Name'`, `'userID'`, `'ID'` and `'class'` are all
  perfectly good identifiers that no row is ever keyed by — they reach a
  transform only through `contract.resolve_name`, exactly like the visibly
  messy `'First Name'`. An `isidentifier()` proxy therefore recognises ONE of
  the two halves of "original header" and silently mis-files the other, which
  is how `field_mapper` came to guarantee a column its own `process` deletes
  (`SchemaConfigModeViolation: missing required fields ['name']` on a correct
  rename, plus a `DeclaredOutputFieldsViolation` when the deleted normalized
  name is another entry's target). The predicate is normalization's FIXED
  POINTS; `value_transform._row_key_aliases` had already learned this and is
  the handling to copy — answer `ExternalHeaderError` (a literal that
  normalizes to nothing names no field, so it is not a row key) and let a bare
  `ValueError` propagate, or the exception class a bad config key raises at
  construction changes. Two traps when auditing this class: (a) a corpus scan
  finds nothing, because a case-variant source is a pure blind spot — zero
  in-tree configs use one, which is precisely why no test caught it; test the
  predicate against `SchemaContract.resolve_name` rather than against
  `normalize_field_name` again, or the assertion restates the implementation;
  (b) the fix moves BUILD-TIME verdicts, and the direction is not the obvious
  one — a wider "unresolved" set makes `field_mapper` ABSTAIN more, which
  through `schema_validation.py`'s `if not vote.fields and not
  vote.participated` clears graphs it used to reject. Measure with a
  HEAD-vs-patched `ExecutionGraph` matrix and check the moved cells against
  the class that already abstains, not against zero.
- **2026-08-20 — EDITING a builtin plugin: `source_file_hash` tracks content,
  `plugin_version` does not.** Section 6 below covers ADDING a plugin; this is
  the edit path, and the two attributes behave differently:
  - `source_file_hash` must be recomputed for ANY content change, not just a
    new plugin. Compute with
    `scripts/cicd/plugin_hash.py::compute_source_file_hash`, and do it AFTER
    `ruff format` — the pre-commit formatter rewraps lines and restales a hash
    computed first. The computation normalises the hash line itself, so it is
    stable once written.
  - `plugin_version` is a STATIC declaration. All 41 builtins sit at `1.0.0`
    and none has ever been changed in place (verified over the whole history,
    2026-08-20) — do not invent a bump for a behaviour change, and do not read
    an unbumped version as an oversight.
  Two traps when verifying: (a) `computed in path.read_text()` is a SUBSTRING
  test that passes on a mere comment — compare strictly,
  `Cls.source_file_hash == compute_source_file_hash(path)`; (b) **no local test
  enforces the hash at all** — a stale hash with a mutated body passed 10,213
  tests. The gate is CI-only, so that strict comparison is the only local
  defense, and a green scoped run says nothing about it.
- **2026-08-20 — a construction-time normalisation in `web/composer/state.py`
  invalidates PERSISTED hashes; it is never a local change** (landed with
  elspeth-da00e1c1cb). `NodeSpec.__post_init__` is advertised as the one
  construction boundary every path routes through, so normalisations keep
  landing there — and each one silently breaks two seams that read bytes
  written by an OLDER build:
  - `restore_owned_composition_state_authority` (`pipeline_proposal.py`)
    requires `to_dict(from_dict(payload)) == payload`. A payload authored
    before your normalisation can never satisfy it again, and it must NOT be
    migrated: `tool_arguments_hash`, `private_arguments_hash` and
    `draft_hash` all bind the raw bytes, so rewriting them trades one
    integrity error for three.
  - `composition_content_hash` hashes `to_dict()` AFTER normalisation, and
    that value is STORED in `PresentBase.composition_content_hash`.
    `sessions/service.py` re-derives and compares it on the fork path, so a
    normalisation retroactively unbinds every stored base binding for the
    shapes it touches. This is the wider blast radius, and it needs no owned
    authority to fire.
  `tests/unit/web/composer/test_state_serialisation_contract.py` pins both:
  content hashes for representative authored shapes, and an AST check that
  every spec's `from_dict` reads EXACTLY its declared dataclass fields (the
  undeclared-field rejection uses `dataclasses.fields()` as the set of keys a
  restore observes). If your change reddens the hash pins, that is the gate
  working — decide what happens to already-persisted states before re-pinning.
  Two traps: (a) an AST gate that looks for `object.__setattr__` is BLIND,
  because house style routes field rewrites through `freeze_fields`
  (`contracts/freeze.py`) — pin behaviour, not syntax; (b) do not "fix" the
  restore by tolerating stale bytes. The coalesce defaults are read from
  `CoalesceSettings.model_fields` precisely so they track the runtime, so a
  payload that omits `merge`/`policy` records no epoch: accepting it lets a
  later default change re-interpret already-reviewed bytes with every hash
  still green. Quarantine, do not migrate.
- **2026-08-20 — a REDUCTIVE transform's output `SchemaConfig` must not carry the
  authored INPUT `fields`** (landed with elspeth-a2bf676e6f). A transform node's
  `schema:` block is the INPUT contract — `BaseTransform._build_output_schema_config`
  documents its argument as "the transform's input schema config (base fields)"
  and instructs reductive subclasses to "drop input-side declarations"
  (canonical override: `BatchStats`, elspeth-f5f798f797). `field_mapper`
  overrode the method but still passed `cfg.schema_config.fields` through, so
  every field it CONSUMES-but-never-emits (a renamed-away source in BOTH modes,
  and anything outside the whitelist under `select_only`) stayed `required` on
  the OUTPUT config. `SchemaConfig.get_effective_guaranteed_fields()` is
  `explicit guaranteed | required declared fields`, so those names were then
  demanded as output guarantees and the transform's own contract rejected its
  own emitted row with `SchemaConfigModeViolation`. Composer-side this surfaced
  as a Rule C "Transform contract violation" on a CORRECT cleanup pipeline, and
  a renaming mapper had NO satisfiable declaration at all under `strict: false`
  (the honest split — guarantee the sources, declare the targets — is rejected
  at construction because `guaranteed_fields` must be a subset of `fields`).
  Two traps when touching this area: (a) a rename target may be declared by
  EITHER name — prefer a declaration written against the emitted (target) name,
  else carry the source's across so the type does not degrade to `any`; (b)
  three existing tests used "declared in `schema.fields` but not selected" as a
  VEHICLE to trip Rule C or the post-emission check — that shape is legal now,
  so re-point such a vehicle at an unguaranteed RENAME rather than deleting the
  test. STILL OPEN, deliberately unbundled: `base_guaranteed` in
  `_build_field_mapper_output_schema_config` reads the explicit
  `guaranteed_fields` tuple only, not `get_effective_guaranteed_fields()`, so a
  `mode: fixed` mapper with no explicit `guaranteed_fields` guarantees nothing
  and Rule C still false-positives on a plain whitelist. Changing it moves DAG
  contract propagation, so it needs its own sweep.
- **2026-08-19 — `plan_pipeline` requires the session schema tracker; aid-supplied
  manifest keys are palette-retained AND escalation-exempt** (landed with
  elspeth-cb3561382e/275e05bf71/ac44757161). `plan_pipeline` takes
  `schemas_loaded`/`mark_schema_loaded` as REQUIRED kwargs — a new call site
  must thread the per-session tracker (`ComposerServiceImpl._mark_plugin_schema_loaded`
  via `functools.partial`), never default it away. Manifest keys supplied by
  authoring aids (`model.catalog`, `expression.grammar` — the closed
  `_AID_SUPPLIED_INFORMATION_KEYS` set) keep their palette tools advertised as
  the oversize escape AND are exempt from no-gain ESCALATION (per-call
  DISCOVERY_NO_GAIN feedback still fires; the turn budget is the doom-loop
  backstop). Extending either set means revisiting both properties together —
  a supplied key whose tool stays advertised without the exemption is a
  request-killer (two no-gain calls = terminal), which is exactly the shape
  review caught here.
- **2026-08-18 — `ToolResult.to_dict()` keys are declared TWICE: the dataclass
  and the redaction manifest** (landed with elspeth-f14aba9686). Adding a
  serialized key to `ToolResult.to_dict()` without declaring it in
  `redaction.py` does not degrade gracefully: `_ToolResultResponseModel` is
  `extra="forbid"`, so every type-driven mutating tool REJECTS the response at
  the audit-persistence boundary (a scoped tool test stays green; the failure
  is in `redact_tool_call_response`). Declare the key on the model (Sensitive
  envelope for payload-bearing keys) AND in `_TOOL_RESULT_OPTIONAL_RESPONSE_KEYS`,
  then regenerate `redaction_policy_snapshot.json` via
  `scripts/cicd/bootstrap_redaction_snapshot.py --write` — never by hand. Know
  the three dispositions: (a) declarative entries with `handles_no_sensitive_data=True`
  have EMPTY `known_response_keys` — the shared-list edit never reaches them and
  the new key aggregates as `REDACTED_UNKNOWN_RESPONSE_FIELD` (value-free, safe,
  +1 telemetry event — the pre-existing `affected_nodes` pattern); (b) declarative
  entries with `handles_no_sensitive_data=False` and NON-EMPTY
  `known_response_keys` (the `set_metadata` / `set_output` / `upsert_node`
  class) DO inherit the shared-list key: each such entry's snapshot hash moves,
  and that churn is what feeds disposition (c); (c)
  `check_redaction_direction.py` can verdict a pure strengthening as `weaken`
  via its conservative same-count rule when an entry's hash moves only by
  gaining a non-sensitive key — that needs the `policy-weaken-justified` label
  + exact-phrase rationale on the PR, not a code workaround.
- **2026-08-18 — guided prompt traps: the palette gate polices the COMPOSED
  prompt, step skills feed THREE surfaces, and the planner context is where
  you explain a redaction** (conventions landed with elspeth-63cf3803e6; the
  palette gate itself predates it — 377bcc9a3, 2026-07-20). (a)
  `test_guided_chat_prompts_name_only_tools_in_their_actual_palette` asserts
  over `load_step_chat_skill(step)` — base.md PLUS the step file — for every
  GuidedStep, so one base.md edit can redden all four step assertions at
  once. Exact pins: `list_sources`/`list_transforms`/`list_models` in NO
  composed prompt; `list_sinks`/`get_plugin_schema` absent from step 1 and
  present in step 2 (steps 3–4 are NOT policed for those two);
  `confirm_wiring` absent from step 4. Say what to do, never which tool not
  to call — a natural phrasing like "don't call `list_transforms` to confirm
  a negative" turns the gate red. (b) A `guided/skills/step_*.md` file
  renders on the PLANNER surface and on the step CHAT surfaces (per-step
  solver and the deferred-intent management chat). The chats receive no
  planner-context enrichments — `unproducible_output_fields` never reaches
  them — so a skill branch conditioned on a planner-context key's ABSENCE is
  vacuously true in every chat session: scope such branches to authoring, and
  give question-answering a hedged variant. (c) When a redaction makes the
  planner burn discovery turns, the fix seam is a static usage line INSIDE
  `guided_redacted_planner_context` (the `output_usage` /
  `reviewed_configuration_usage` precedent) — adjacent to the confusing keys,
  zero new egress — not the system prompt. The phrasing constraints
  (RESTORED, never "owns") live on that key's comment in planning.py; read
  them there before rewording. The projection is pinned by full-dict equality
  in `test_proposal_audit_projection.py`, so any added key is a deliberate
  test update, and the canary assertions above the pin prove the addition
  leaked no private value.

- **2026-08-17 — a full-suite run in the SHARED checkout is not evidence unless
  HEAD is unchanged across it, and a worktree A/B UNDER-COLLECTS**: two ways a
  whole-tree measurement lies. (a) `pytest tests/` takes ~18 minutes; four sibling
  commits landed inside one such window on 2026-08-17 and the run reported **456
  failures** across engine/pipeline/e2e that did not exist before or after (a
  representative slice re-run immediately after: 22 passed). Record
  `git rev-parse HEAD` BEFORE and AFTER a long run; if they differ, a red result
  is uninterpretable — re-run rather than diagnose. (b) The obvious fix — run the
  A/B side in a `git worktree` — silently changes what is collected: `evals/*` is
  git-ignored except for tracked re-includes, so a fresh worktree has no
  `evals/composer-rgr`, `composer-harness`, … and every suite that GLOBS those
  assets collects fewer tests there (measured: `test_convergence_scenarios.py`
  11 vs 32, `test_paths.py` 22 vs 40, `test_execution_repository.py` 148 vs 161).
  A worktree test-count delta is therefore NOT attributable to your change. To
  attribute a count honestly, diff per-file collected counts
  (`pytest --collect-only -q | sed 's/::.*//' | uniq -c`) between the two trees and
  read the per-file rows, not the total. Worktree e2e recovery tests also fail on
  capture-root binding, so a worktree pass/fail is its own instrument.

- **2026-08-17 — a directory-scoped test `conftest.py` that mutates `sys.path`
  is PROCESS-GLOBAL, not directory-scoped**: `tests/unit/evals/composer_battery/
  conftest.py` (Task 7 of the composer-battery build, elspeth-composer-battery)
  is the first such shim under `tests/unit/evals/` — it `sys.path.insert(0, …)`s
  `evals/composer-battery/` so tests can `import drive_battery` (a module that
  lives in a hyphenated, non-package directory). pytest loads a directory's
  `conftest.py` once per worker process, but the path mutation persists for
  the REST of that worker's session — every later test module collected on
  the same xdist worker resolves a bare top-level import against
  `evals/composer-battery/` FIRST, ahead of site-packages and the repo root.
  That directory holds generically-named modules (`report.py`, and Task 8's
  `planner_probe.py`) that could shadow an unrelated `import report` elsewhere
  in the suite. Verified 2026-08-17: no test currently does a bare
  `import report`/`from report import`, so this is latent, not live — but the
  next agent adding a `tests/unit/**/conftest.py` with a similar `sys.path`
  insertion must grep for a same-name collision first
  (`grep -rn "^\s*import <name>\b\|^\s*from <name> import" tests/ src/ evals/`),
  and prefer `sys.path.append(...)` over `insert(0, ...)` unless import
  priority is actually required, so a same-named third-party or repo module
  keeps precedence.

- **2026-08-17 — a NEW runtime rejection needs a Stage-1 disposition (whole-tree
  gate)**: `tests/unit/scripts/cicd/test_runtime_rejection_parity_gate.py`
  AST-enumerates every `raise <Exception>(...)` under `src/elspeth/core/dag/`
  and `src/elspeth/core/config.py` PLUS every declarative pydantic
  `Field(min_length=/max_length=/gt=/...)` constraint on those settings
  models, and requires each site to carry a reviewed disposition in
  `config/cicd/runtime_rejection_parity.yaml` (`mirrored` with a real Stage-1
  `error_code` or `fn:<validator>` counterpart, `abstains`, `structural`,
  `not_authorable`, or `unmirrored` under a ratchet ceiling — 10 today,
  elspeth-96e2dd023f). Adding a rule to the DAG builder or a settings
  validator therefore fails the gate until you run
  `.venv/bin/python scripts/cicd/runtime_rejection_parity.py --write` and
  adjudicate the seeded entry — that is the point (elspeth-2ed41f0a4a: Shape
  17 landed a runtime rule with nothing requiring its authoring counterpart).
  Rewording a message re-keys the site (`--write` drops the stale entry and
  seeds the new one; carry the adjudication across). Never hand-edit a `key`.
  Sibling conventions landed with it: Stage 1 mirrors the runtime NAME/LABEL
  rules by calling the runtime's own validators (`_composer_node_id_validation_message`,
  `_routing_label_errors` -> `_validate_connection_or_sink_name` /
  `validate_composer_output_name`), so fixture node ids/labels must be
  runtime-valid (leading letter, <=38 chars, not `fork`/`continue`/`on_success`,
  sinks lowercase) — a UUID or a gate named `fork` now fails Stage 1 exactly as
  it fails `settings_load`; fix the FIXTURE, never relax the mirror. Coalesce
  `merge: select` and `policy: quorum` are rejected as unauthorable (no
  `select_branch`/`quorum_count` on NodeSpec); `best_effort` needs
  `timeout_seconds`. Cycle detection (`_node_topology_cycle`) is whole-graph.

- **2026-08-17 — a guided correction that writes a routing scalar must move its
  SINK MIRROR EDGE in the same materialization** (elspeth-a0a830fc95): scalar
  routes are the runtime authority and sink-targeting edges are their mirror
  (elspeth-67b44040ee); the guided public `connections` projection derives every
  reviewable connection from the scalars and never reads `edges`. Every
  correction path in `guided/planning.py`
  (`materialize_guided_authorized_candidate`) therefore ends by calling
  `_reconcile_draft_sink_mirror_edges` — edges follow scalars, retargeted onto
  the slot's current sink or dropped when the slot no longer names one, and
  never invented (an undrawn route is inferred from the scalar, so absence
  cannot lie the way a stale edge does). Add a new correction path and you must
  call it, or Stage 1 fails closed on `edge_route_mismatch` against a delta
  with no repair surface — deterministic REPAIR_EXHAUSTED. Two traps: (a) the
  edge-correction arm is scoped to the SELECTED slot only, because
  `_edge_preserved_state_fingerprint` hashes the whole document and proves
  nothing else moved — the slot and its mirror are ONE authority, so that
  function REMOVES (never marker-substitutes) the slot's mirror edges on both
  sides; a wider sweep there is an undetected out-of-authority edit; (b) do NOT
  "fix" the inverse half by clearing a dropped producer's scalar. Measured
  2026-08-17, every producer kind: transform `on_success=None` ->
  `transform_missing_on_success`, `"discard"` -> `transform_on_success_dangling`;
  deleting a gate route key -> `gate_route_labels_mismatch` (emptying `routes`
  -> `gate_missing_routes`); source `"discard"` -> `source_on_success_dangling`.
  There is no valid cleared value, so clearing converts a benign undrawn route
  into a rejection the delta cannot repair. The node arm sweeps ALL of the
  owner's slots (as `upsert_node` does), which means a provider-authored edge
  patch retargeting a GATE route to a different sink is snapped back rather than
  honoured — correct, because `routes` is deliberately absent from the
  node-patch schema and gate routing is the edge-correction surface's job.
  Every pre-existing correction fixture used `edges=()`, which is why the suite
  could not see any of this; new fixtures live beside
  `_mirror_edge_correction_predecessor`.

- **2026-08-15 — commencement gate conditions have separate execution and
  audit forms**: only the raw configured expression enters `ExpressionParser`.
  Every `CommencementGateFailedError`, successful `CommencementGateResult`, and
  persisted preflight result carries the AST-derived rendering from
  `redact_commencement_gate_condition`: direct subscript/`.get()` string keys
  and dict keys stay visible for structural diagnosis, while every other
  string literal — including values nested inside a composite lookup key —
  becomes `<redacted-string-literal>`. Config admission catches only the
  parser's syntax/security rejection types and hides the raw Pydantic input;
  parser AST diagnostics can contain raw literals. Do not restore the raw
  condition in a downstream formatter or use heuristic secret-pattern
  matching; arbitrary literals are the protected class.

- **2026-08-15 — union collision audit provenance stops at field and branch
  identity**: `_merge_data` must retain raw `collision_values` internally so
  `first_wins` can restore the configured branch's value, but
  `build_coalesce_merge` must never pass those values into `CoalesceMetadata`.
  Stable unsalted hashes and Python type names remain value-derived sensitive
  material: low-entropy candidates can be recovered offline and correlated
  across runs. Persist `union_field_collisions` and `union_field_origins`; keep
  `union_field_collision_values` absent from new `context_after_json` records.

- **2026-08-15 — value-source findings are response and log egress**:
  catalog-membership and sibling-derivation failures flow into the Composer
  `/validate` response, exception text, check-detail/log surfaces, model repair
  prose, and persisted composition-state `validation_errors`. Never render,
  summarize, hash, measure, or call `repr` on a configured value there: even a
  short ordinary-looking scalar or a derived length can disclose private
  configuration. Keep component, field, sibling-field, catalog, and remediation
  relationships in fixed structural prose so failures remain actionable without
  becoming an echo channel.

- **2026-08-15 — every `NodeStateGuard` caller names the scope it owns**:
  `auto_fail_phase` is required, has no default, and is a closed runtime-and-
  static vocabulary because the guard spans materially different work:
  transform execution, gate evaluation/routing, aggregation flushes, and
  pre-attempt shutdown persistence. The value is durably recorded in
  `ExecutionError.phase` and shown verbatim to operators; a generic, falsey,
  or misspelled fallback silently creates false attribution. A new guard site
  therefore requires deliberate vocabulary extension plus caller-path tests.
  Keep explicit inner failure phases authoritative: once the caller has
  persisted a terminal state, the guard must stand down rather than overwrite
  it with an auto-generated failure.

- **2026-08-15 — engine span correlation belongs to the durable run lifecycle**:
  production engine spans are completion events emitted through the existing
  `TelemetryManager`; do not create a second tracer/provider lifecycle beside
  its exporter fan-out. A fresh run binds trace identity to persisted
  `Run.started_at`. Resume and follower paths use `SpanFactory.trace_scope(...)`
  and deliberately do not emit a second whole-run span or fabricate a leader
  parent. SQLite may return that timestamp without `tzinfo`; normalize it as
  UTC at the span boundary, but tolerate cross-host wall-clock skew. Row spans
  live at the universal scheduler-claim seam so fresh, resumed, and follower
  work share one path, and an operation span stays open through the engine's
  validation, authority, disposition, and terminal audit work — plugin return
  alone is not success. Row and aggregation parents are path-dependent: fresh
  ingestion may inherit source/row context, while late leader-drain, resume,
  and follower work uses durable run correlation. Correlate spans to Landscape
  through opaque run, node, token, and batch identities; audit-only row-content
  hashes must not enter engine span attributes. Row-event producers retain real
  hashes for in-process audit correlation, but `TelemetryManager` projects
  `RowCreated.content_hash` and `TransformCompleted` hashes to `None` before
  observers or exporters see them. It reconstructs exact owned base events so
  a subclass cannot smuggle sibling hash metadata across the boundary; never
  substitute a shared redaction marker that downstream could treat as a real
  hash. A handled exception already
  present in the caller is not a span-body failure, and a telemetry callback
  failure must not replace or rewrite an active workload failure. Exporters
  retain fresh-run trace origin until both `RunFinished` and the enclosing run
  span completion arrive, in either order; joined/resumed runs clear on their
  sole terminal event.

- **2026-08-15 — a selector lane may contain SEVERAL trusted profile probes**:
  the state-engine profile reporter accepts repeated observations that agree on
  every profile-identity field (case, store, deployment, backend version,
  probe shape) and binds the FIRST probe test as the report's
  `deployment_probe`; it fail-closes only on disagreement. Do not "fix" a
  multi-probe lane by deleting probe tests or splitting the lane — the
  single-observation invariant is about one run claiming two DIFFERENT
  profiles. Discovered by the first full-lane single-invocation evidence run
  (Task 12); per-cohort runs never exercised two probes together. Also:
  evidence venvs must be built on the release interpreter (Python 3.13 —
  `ci.yaml` maintains 3.12/3.13); a bare `uv venv` picks the newest local
  Python (3.14) whose annotation semantics fail ~11 suite tests spuriously.

- **2026-08-12 — a live-evidence artifact cannot authenticate its own upload
  digest**: the final GitHub artifact/archive digest exists only after upload,
  so embedding it in `manifest.json` is circular and self-declared hashes are
  not producer authentication. Ingestion selects the artifact through the
  read-only Actions API, downloads that API record's archive, verifies the
  API-reported digest over the downloaded bytes, safely admits the exact five
  regular members, and byte-compares them with the supplied directory. Reject
  duplicate/traversal/extra/encrypted/oversized or compression-bomb members.
  GitHub's archive endpoint redirects to a different origin: strip the bearer
  token on every cross-origin redirect, never forward it to the signed blob
  host.

- **2026-08-12 — PB-09 plugin variants are a three-way exact-set contract**:
  `scripts/state_engine_plugin_matrix.py check` derives the closed variant set
  from production-owned Pydantic discriminators and registries, constructs
  every variant through real config validation, and compares the mechanical
  discovery projection with
  `tests/golden/state_engine/plugin_lifecycle_matrix.json`. The discovery suite
  separately pins live plugin keys, golden variants, and v3 PB-09
  `(plugin_key, variant_id)` pairs. Adding a plugin or a supported auth/provider
  mode requires updating the reviewed golden fields and v3 PB-09 cases together;
  `render-skeleton` deliberately exits nonzero while any new reviewed field is
  `UNCLASSIFIED`. The golden is reviewed evidence, never variant authority.

- **2026-08-12 — follower teardown has one exit seam and partial startup is
  tracked explicitly**: `FollowerProcessor.run()` stops its heartbeat before
  departing the single-use worker on every exit, including unexpected
  traversal exceptions. Do not add a new exception arm that departs early or
  bypasses the common `finally`; exact-once departure and stop-before-depart
  ordering are pinned. CLI follower startup records a transform or sink only
  after its `on_start()` returns. Pass those exact started subsets to
  `cleanup_plugins`; never call `on_complete()` or `close()` on the plugin
  whose startup raised, or on later plugins that were never started.
  `cleanup_plugins` also requires an explicit `pending_exc`: preflight and
  startup `except` arms pass the bound exception they re-raise, while
  steady-state and follower scopes initialize a local to `None` and set it
  only from `except BaseException` around the exact scope whose exception
  leaves that boundary. A normal return passes `None`, even when the boundary
  was invoked inside an outer handled `except`. Never derive cleanup policy
  from `sys.exc_info()` or `sys.exception()` in the helper or a `finally`
  caller. This explicit input does not change Tier-1 cleanup precedence,
  lifecycle callback ordering, or exact partial-startup subset cleanup.

- **2026-08-12 — Python 3.14 annotation closures expose class namespaces to
  Runtime-VAL**: PEP 649 `__annotate__` functions close over `__classdict__`
  and use `LOAD_FROM_DICT_OR_GLOBALS`; never normalize that whole dictionary,
  because it contains unrelated interpreter state such as `_abc_impl`.
  Normalize only the exact names read by supported bytecode shapes, including
  whether each binding resolves from class, module globals, or builtins, and
  fail closed on any unrecognized dictionary use. Slot member descriptors bind
  by exact declaring `module:qualname` plus descriptor name. Python 3.14 also
  emits `slice` objects as code constants, so preserve all three normalized
  bounds rather than falling back to repr or narrowing supported Python.

- **2026-08-15 — adding a field to the composer spec/tool contract fires THREE
  pins, and the third is a PRODUCTION wire decoder, not a test**: any new
  field on SourceSpec/NodeSpec/OutputSpec or a composer tool argument model
  must ALSO land in (a) the `canonical-field-inventory` table in
  `src/elspeth/web/composer/skills/pipeline_capabilities.md`
  (test_capability_skill_identity derives the real schema and diffs the table),
  (b) the redaction-policy snapshot — regenerate via
  `scripts/cicd/bootstrap_redaction_snapshot.py --write` and review that only
  hashes moved, never `sensitive_path_count`, unless you intended a new
  Sensitive path — and (c) the frontend's strict guided wire decoder
  (`frontend/src/api/guidedDecoder.ts`, `decodeCompositionState`), whose
  `exactRecord` key lists reject any unenumerated key AT RUNTIME. Missing (c)
  is invisible to every backend suite and to frontend tests that stub
  `composition_state: null`: the first guided re-plan after deploy emits the
  new key and every `/guided` response becomes "received but could not be
  read" while the server keeps returning 200 (elspeth-b48212113e, fixed
  7694f5f1b). Grep the frontend for `exactRecord` lists naming your
  sibling keys before calling a wire-contract change done. Serialise optional spec fields as omitted-when-None so
  pre-existing persisted states and their `composition_content_hash` values
  stay byte-identical (see `description`, 80fa17fed). The guided planner's
  advertised full-document schema derives from the registered `set_pipeline`
  JSON schema via `canonical_set_pipeline_schema()`, so extending that schema
  + the redaction.py models covers the planner lane automatically.

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

**Do NOT rewrite the probe resolver. That road is closed** (decided
2026-08-09; re-walked by accident and re-confirmed 2026-08-16). Two full
attempts were built and rejected by independent review: a partial-CPython
state model (Freezes 1-5, 5,216 lines, never merged) and then a sparse-SSA
definition/phi/value-graph solver (Freeze 6). The Freeze 6 rejection found
late-global, try/match, annotation-timing, loop-header, star-import and
callee/argument misses, plus non-monotone `PROJECT` output and `CALL_RESULT`
role collision — while its adversarial *scaling* was fine. The systems review
classified Freezes 1-5 as a Fixes-that-Fail / Limits-to-Growth loop: local
cases converge while the state model expands faster, producing new fail-open
families. `inventory.py` is therefore the single authority, and the standing
stop rules (`elspeth-02cd60d8cd`) are: **no CFG/SSA/history/replay/lazy-cache/
object-emulator growth, and <=2.5x runtime per input doubling.** New semantic
coverage lands as narrow, ticket-level RED tests against the existing visitor —
see the open siblings `elspeth-682e0c6581` (definition-header replay),
`elspeth-f1def53d38` (PEP 695/696 scopes) and `elspeth-2a72512454`
(destructured RHS alias evidence).

That entire history exists ONLY in `filigree get-comments elspeth-de6f571887`
(a CLOSED issue); git messages, this file and the module said nothing until
now, which is exactly why it got re-attempted. **Read that comment stream
before touching resolution.**

Three facts a future attempt will want, all measured 2026-08-16:

- **Corpus agreement cannot validate a change here.** A candidate resolver
  matched the shipped one on all 2,866 files (474/474 sites, zero false
  positives) while still being fail-open on late-global. The evasion shapes
  are absent from a tree that currently passes the gate, so the oracle must be
  adversarial hand-written cases; the corpus run only answers "would today's
  findings change".
- **Two fail-open holes were closed in place on 2026-08-16**
  (`elspeth-34ac84b4b6`, `elspeth-682e0c6581`), and the mechanisms are now
  conventions you must not undo:
  - *Loop heads are a fixpoint.* `_loop_head_bindings` /
    `_comprehension_head_bindings` join the body's reachable states back into
    the loop head until stable, so a probe used before its rebind inside a
    `for`/`while`/comprehension is visible on iteration 2+, `continue`/`break`
    carried states count, and boundary provenance is dropped for any name the
    body can rebind. It terminates only because binding targets are finite:
    `_MAX_TARGET_DEPTH` collapses any dotted target deeper than 8 segments to
    `<shadowed>`. **Never build an unbounded target string** (`node =
    node.next` in a loop hung the whole-tree scan before that cap existed).
  - *Resolution keeps evidence.* `_resolve_binding_expression` no longer
    returns an empty set for an unmodelled shape: every *uncalled* probe
    reference inside it (`wrap(getattr)`, `partial(getattr, o)`, `[getattr]`,
    `probes[0]`, `for p in probes`) flows out beside the `<shadowed>` marker,
    so the eventual call is inventoried. A *called* probe contributes nothing
    (its result is arbitrary; the call itself is the site). Consequence for
    code you write: passing `getattr`/`hasattr`/`getattr_static` as a value is
    inventoried where the carrier is called — do it only where you would
    accept a baseline entry.
  - *Definition headers replay in CPython order.* `definition_header_expressions`
    is the single evaluation-order authority for both the live visitor and the
    deferred projection: decorators, positional then keyword defaults, then —
    outside `from __future__ import annotations` — signature annotations in
    CPython's order (`args` BEFORE `posonlyargs`, vararg, kwonly, kwarg,
    return); classes: decorators, bases, keywords. Variable annotations execute
    after the value is stored, only outside function bodies, and never under
    PEP 563 (`_ExecutionContext`); unexecuted annotations are still inventoried
    (dead-code style, deferred bindings for stringized ones) but their walrus
    effects never touch the live state. Default captures happen at each
    default's own evaluation point. Modelled semantics are 3.12/3.13; 3.14
    (PEP 649) defers annotations and rejects a walrus inside one.
    `test_definition_header_expression_order_matches_the_running_interpreter`
    pins the enumerator to the interpreter — note it must `compile(...,
    dont_inherit=True)` because the test module itself imports
    `annotations` from `__future__`, which `compile` otherwise inherits.
  - *PEP 695/696 annotation scopes are modelled* (`elspeth-f1def53d38`).
    Bounds, constraints, PEP 696 defaults and `type` alias values are LAZY:
    inventoried against the deferred bindings, with the declared self-name
    bound and — when the scope is immediately inside a class body — the class
    dict consulted first (position-aware: `_ClassBodyCursor` projects the body
    once and suffix-joins, so states from the def onward count and names the
    class bound before it shadow the globals). Generic annotations and class
    bases/keywords are EAGER inside the annotation scope (type params shadow,
    the enclosing class IS visible); generic *defaults* evaluate in the
    enclosing scope (type params do NOT shadow them, a walrus there binds
    outside). Type params are closure cells of the body and every nested
    scope (`_push_binding_scope` shadows every active one and skips
    `"annotation"` frames like `"class"` frames). A walrus is a SyntaxError in
    bounds/annotations/alias values, so the projection needs nothing. 3.12 =
    3.13 on all of this (verified with an interpreter oracle); do not add a
    lazy-force cache, a CFG, or a second evaluator to "improve" it.
  - Still open in this family: `elspeth-2a72512454` (destructured literal RHS
    timing). Not modelled and not claimed: `getattr.__call__(...)`,
    `a, b = wrap(getattr)` (destructuring a laundered value), reflective
    laundering (`getattr(builtins, "getattr")` — the outer call is itself a
    site).
- **`probe_shape` is invariant under a resolver change** — it is computed from
  the AST node and kind, neither of which passes through resolution (verified:
  354 distinct / 474 total digests, zero differing files). That is what makes
  resolver work safe to attempt at all, since drift would reset the baseline's
  39 human adjudications; land the digest comparison as a test before changing
  anything (the 2026-08-16 change was measured this way: 488/488 sites, zero
  digest/kind/amnesty drift, whole-tree 65.0s → 64.8s after a two-state fast
  path in `_join_binding_states`). Cost context: one whole-tree scan is ~65s
  and `test_masquerade_gate.py` runs four of them (~6 min), ~12% of which is
  a separate quadratic with a ~5-line fix (`elspeth-df09888129`). The
  per-statement state copy in the possible-bindings model is itself
  quadratic in suite length (~3–4× per input doubling on flat 1600-statement
  inputs, before and after); the stop rule is about not making that class
  worse, and any loop-head work multiplies it by the fixpoint pass count.

### 3. Trust-tier lint corpus (standing)

`elspeth-lints check --rules all --root src/elspeth` is fail-closed (exit 1,
~3.1k-line corpus, tracked as elspeth-13f0cc04fb). Do NOT expect zero and do
NOT try to clear it. The obligation is: capture the corpus BEFORE your
change, capture it AFTER, and diff — you must add nothing. Never hand-edit a
`judge_metadata_signature`; never shape code to reduce signature churn.

**Exception — release closeout (2026-08-17, 0.7.2).** The "do not clear it"
rule above scopes to *ordinary feature work*, which is the same qualifier
AGENTS.md uses. When a release package is being made ready for merge, clearing
the corpus IS the work and the operator signs at the end. During 0.7.2 closeout
the standing ban is lifted by the operator; treat the corpus as a worklist, not
a fixed backdrop. Two things do NOT relax, ever: never hand-edit a signature,
and never shape code to reduce churn. Measured 2026-08-17 for scale — the
tier_model allowlist held 606 entries, 351 requiring action (178
`NO_MATCHING_FINDING` orphans, 127 `AST_PATH_BINDING_DRIFT`, 39
`IDENTITY_PREFIX_REPLACEMENT`, 35 `PRE_JUDGE`, 6 `SCOPE_BINDING_DRIFT`, 1
`SOURCE_FILE_MISSING`; 229 of them in `web.yaml`). Those binding failures are
INVISIBLE to `check --rules trust_tier.tier_model`, which reported only 6
per-file `max_hits` overflows — use `mcp__elspeth-judge__verify_signatures` for
the signature-health surface, and remember it is shape-only without the key.
Stage the bundle LAST: bundles are exact-source-bound to Git HEAD plus a digest
of every scannable file, so any sibling edit that shifts an AST position
invalidates one already staged.

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

### 7. CSS barrel structural gates (2026-08-11)

Every custom property referenced with `var()` must also be defined in a
stylesheet; an inline React style does not satisfy the whole-tree token gate.
Do not add a standalone `@media (forced-colors: active)` block before the
canonical final block in `styles/themes.css`: the whole-barrel contrast gate
treats the first block as canonical and will inspect only that partial corpus.

### 8. Playwright auth state is worktree-global (2026-08-12)

Never run two Playwright commands concurrently in the same worktree. Global
setup rewrites the shared `tests/e2e/.auth/user.json`; distinct backend and
frontend ports do not isolate that file, so otherwise independent runs can
corrupt each other's authenticated state. Run every Playwright suite
sequentially per worktree.

## Recent conventions (prune when archived)

- **2026-08-17 — worker-pool admission follows the WORKER's lifetime, and the
  preflight coordinator owns the caller's budget** (elspeth-5269b43bca /
  elspeth-8607553d3b): `run_sync_in_worker` bounds outstanding submissions at
  `ADMISSION_CAPACITY` (16 running + 16 queued), released when the sync work
  actually finishes — never when the awaiter times out or is cancelled. Past
  capacity it waits `ADMISSION_WAIT_SECONDS` then raises
  `AsyncWorkerAdmissionTimeoutError` (a `TimeoutError` subclass, so every
  deadline arm already classifies it as its own TIMEOUT). An awaiter that
  gives up while its submission is still QUEUED drops it — it never runs; a
  RUNNING one completes but its outcome is discarded (the cancelled wrapper
  also carries the old elspeth-e4949acbe1 "no unretrieved exception"
  contract, the drain callback is gone). Consequence: a write that MUST land
  goes through a shielded/deferred-cancellation wrapper
  (`_await_custody_settlement`, `_await_pipeline_staging_write_with_deferred_cancellation`,
  `_run_sync_with_post_commit_projection`); do not rely on "the worker runs
  anyway" for a bare `run_sync_in_worker` call. `RuntimePreflightCoordinator.run`
  takes the per-caller budget as `timeout=` and returns
  `RuntimePreflightFailure(TimeoutError)` on expiry while the in-flight entry
  stays until the sync preflight completes, so a same-key retry JOINS it.
  Never wrap `asyncio.wait_for` inside the shared worker coroutine again —
  that made the timeout the shared task's outcome and evicted the entry while
  the thread still ran (every retry queued another hung worker).

- **2026-08-14 — adding ANY index to the Landscape metadata is a
  delete-and-recreate boundary, and the epoch bump has a docs/website tail**:
  `_validate_schema` compares the FULL metadata shape, not just
  `_REQUIRED_INDEXES`, so a new `Index(...)` in
  `core/landscape/schema.py` makes every existing `audit.db` refuse to open
  with "Landscape database schema is outdated" — a `create_all` on an existing
  table does NOT add it, so there is no self-heal. Bump `SQLITE_SCHEMA_EPOCH`
  with an epoch-history entry, and expect the pins to fan out well past
  `src/`: three test assertions
  (`test_schema_epoch_and_required_columns`, `test_token_ownership_run_scope`,
  guided `test_schema9_epoch`), `CHANGELOG.md`, `website/get-started.html`
  ("29 → NN", pinned by `test_release_site_contract`),
  `docs/guides/sharing-pipelines.md` (pinned by
  `test_release_version_surfaces`), and `docs/product/current-state.md`.
- **2026-08-14 — a two-column equality join needs a two-column index, or
  SQLite guesses wrong**: an audit database has no `sqlite_stat1` (nothing runs
  `ANALYZE`), so when a join offers `run_id=?` AND `token_id=?` and each column
  has its OWN single-column index, SQLite's fixed selectivity guess cannot tell
  that one matches an entire run and the other matches a handful. It picked
  `run_id` and turned the run-accounting census into a nested scan: 618s to
  project one 60k-token run through `GET /api/sessions/{id}/runs`
  (elspeth-c675c8c2d9). Two lessons: prefer deriving a per-token census from
  the SMALL table and subtracting against a count (`token_outcomes` carries a
  composite FK to `tokens`, so "no decision recorded" is arithmetic, not an
  anti-join), and remember a SQLAlchemy `.subquery()` referenced by N separate
  `conn.execute()` calls is executed N times — this one was paid four times
  over. Cost regressions here are testable without wall-clock flake: a SQLite
  progress handler attached on the engine's `checkout` event counts VM steps,
  and asserting a RATIO across two data scales discriminates linear from
  quadratic (see `test_accounting_cost_grows_with_token_count_not_its_square`).

- **2026-08-13 — a live region must PRE-EXIST its content, and that rule is
  not polite-only**: the node must be mounted before the text appears, and
  only the text may change. Inserting a region that already carries its text
  is the form with documented AT failures — for ASSERTIVE regions as much as
  polite ones, so do not "fix" a `role="alert"` by making it conditional on
  reliability grounds. Test the MECHANISM, not the symptom — and note these
  are TWO defect classes with different tells, both of which were live here:
  (a) **node-replacement blindness**: re-querying by test id passes even when
  React REPLACED the node, so hold the element and assert
  `expect(after).toBe(before)` across the transition. INVISIBLE without a
  mutation test (`RunOutcomeNotice.test.tsx`, `AcknowledgementStack.test.tsx`).
  (b) **never performed the transition**: the body mounts with the end state
  already seeded, so the scenario the title names never happens. VISIBLE BY
  READING — a test whose title names a transition and whose body has a single
  `render()` is not exercising one. This is the worse class: node identity is
  the mechanism, the transition is the EVENT, and a test missing the event
  never reaches the mechanism at all. It was live in `ProgressView.test.tsx`,
  the declared M07 announcement authority (fixed da146cd67); suite-wide sweep
  is elspeth-3f40c9aba2.
  **`key={...}` is the cheapest guard-check there is**: force a remount, and if
  the identity assertion does NOT redden it is miswritten. That is what turns
  an existence pin into a truth pin.
- **2026-08-13 — pick live-region politeness by CONSISTENCY WITH THE DECLARED
  AUTHORITY, not by how bad the news is**: `ProgressView` announces all five
  terminal run statuses — `failed` and `cancelled` included — through ONE
  polite `role="status"`. A second App-level region escalating those two to
  assertive made the same event *more* urgent when the operator had looked
  away than when watching it, and assertive cuts off the current utterance
  without re-reading it. A finished background run is a WCAG 4.1.3 status
  message, not an action-forcing alert. A permanently-mounted second assertive
  node also makes every singular `getByRole("alert")` in the tree ambiguous
  (this fired: it broke an unrelated App recovery-panel test). If something
  ever must interrupt, build ONE app-wide announcer owning a single shared
  assertive node — never a second per-feature region. Also: `components/ui/
  AlertBanner.tsx` assigns `role="alert"` to strong tones, so borrowing the
  `.alert-banner` CSS classes is fine but swapping in the COMPONENT silently
  reintroduces an assertive region.

- **2026-08-13 — prose that names a control must derive WHICH controls exist,
  never assume**: review-card labels vary by interpretation kind
  (`llm_prompt_template` renders "View prompt" + "Approve", never
  "Acknowledge"; "Change…" only where `supportsAmendment`). Both prose
  surfaces — the `ChatInput` placeholder and the `subscriptions.ts` system
  note — go through `characterisePendingControls` in
  `components/chat/acknowledgementLabels.ts`, whose invariant is ONE-WAY:
  never name a control the pending card(s) do not render (a mixed set falls to
  control-free wording). That module is a deliberate LEAF (no React, no store)
  because `stores/subscriptions.ts` imports it and was otherwise the tree's
  only store→components edge. Same one-owner shape:
  `components/execution/runTerminalPhrases.ts` owns the terminal-run vocabulary
  that both `ProgressView` and `RunOutcomeNotice` speak.

- **2026-08-13 — a version row's wire projection is REDACTED, so "no change"
  needs the guided blob axis**: `_state_response` runs
  `redact_guided_snapshot_storage_paths`, which overwrites a guided committed
  source's `path`/`file` carriers with a CONSTANT sentinel, so two versions
  differing only in which input file the pipeline reads are byte-identical
  across every content field. `versionLabels.isSnapshotOnly` therefore also
  compares `composer_meta.guided_session.reviewed_sources` blob bindings —
  BOTH the surviving `options.blob_ref` and any `blob:<uuid>` carrier (the
  sentinel arm keeps no `blob_ref`). That is the ONLY thing under
  `composer_meta` allowed to move the verdict; everything else there is
  bookkeeping. `composer_meta` is untrusted wire data, so it is PARSED
  (ADR-032) with an explicit unreadable arm that fails closed. The label says
  "no visible change", not "no pipeline change" — the client can only claim
  what the projection shows; a backend per-version content hash is what would
  earn the stronger word.

- **2026-08-13 — aggregation members' live BUFFERED acceptances keep the
  ORIGINAL batch_id across crash-retry**: `handle_incomplete_batches` retries a
  dead EXECUTING/FAILED batch on a NEW batch linked through the durable
  `batches.retry_of_batch_id` chain and copies `batch_members`, but the
  acceptance-time BUFFERED `token_outcomes` rows are immutable history and
  still name the original batch. Any consumer proving "this member is
  buffered into this batch" — the `complete_aggregation_result` receipt
  writer and the restore receipt validators — must bind against the whole
  retry lineage (`core/landscape/batch_lineage.batch_retry_lineage_ids`),
  never `token_outcomes.batch_id == batch_id` alone; the strict form bricks
  every resumed EOF/flush-fault recovery. Do not "fix" this by writing a
  second live BUFFERED row at restore: duplicate live acceptances are
  refused as corruption by `_derive_restored_batch_id`.

- **2026-08-12 — planner calls and semantic attempts are paired, not counted
  positionally**: a physical provider transport failure has no semantic
  attempt. Every response-bearing planner call (`success` or
  `malformed_response`) has exactly one adjacent `planner_attempt_audit` row,
  whose logical `ordinal` is contiguous even when physical
  `planner_call_ordinal` values have retry gaps. Each `plan_pipeline` request
  restarts both ordinal spaces at 1, so a session transcript can contain
  multiple valid ordinal-reset cohorts. Persist each request's LLM calls,
  attempts, then tool invocations through the existing atomic audit writer;
  never infer attempt/call ownership by array position, and never turn an
  unavailable or malformed audit view into zero-call evidence.

- **2026-08-12 — restricted planner terminals carry schema and materializer
  custody together**: `PlannerTerminalContract` owns the exact schema advertised
  on every normal, repair, and escape-hatch turn plus the function that expands
  that admitted request shape into the canonical pipeline. Restricted contracts
  also carry a request instruction that tells the provider to follow the
  advertised delta rather than the shared core's full-document language. If the materializer
  restores server-owned source, node, or output configuration, return
  `PlannerTerminalMaterialization` with those component refs; materialization
  happens before the ordinary candidate finalizer, so relying only on the
  finalizer diff would expose private validator detail in repair feedback.
  Freeform and guided-full keep the canonical identity contract. Reviewed
  guided initial/correction requests select an authority-derived delta, while
  prose amend/replace remains full-document authoring.

- **2026-08-12 — PostgreSQL token-lock classification needs a fresh statement
  after a lock wait**: do not combine `NOT EXISTS(token_outcomes)` and
  `SELECT ... FOR UPDATE` when deciding token fate under READ COMMITTED. The
  predicate can retain the statement's pre-wait snapshot after a competing
  outcome writer commits. Lock token rows first in stable order, then classify
  outcomes in a second statement. Every later outcome writer must re-check
  for an existing `ABANDONED` row after acquiring the same token lock. SQLite
  cannot prove this protocol because `FOR UPDATE` is inert there; retain the
  independent PostgreSQL race tests for both lock winners.

- **2026-08-12 — every successful aggregation completion owns an epoch-32
  result receipt**: validate the plugin output and declaration contract before
  completing anything, then commit the node, batch, ordered payload refs, and
  exact member actions in one transaction. PostgreSQL writers lock member
  tokens first, then node state, then batch. Transform results use a consumed
  member as the expansion parent; passthrough results carry one output per
  member and retain the original token identities; empty results carry no
  output refs. Restore must load and purely validate every candidate receipt
  before it mutates any candidate. For empty results, terminal member outcomes,
  branch losses, and BLOCKED-to-TERMINAL scheduler transitions share one barrier
  transaction. Do not notify or fire a downstream coalesce/row_union from the
  empty-routing plan: replay the durable loss ledger only after that transaction
  commits, otherwise a failed aggregation commit can strand a consumed sibling
  barrier or lose its merged output. Payload retention and affected-run
  accounting must include the receipt refs.

- **2026-08-12 — completed barrier effects are continuations, not late
  arrivals**: aggregation expansion receipts and completed coalesce-effect
  receipts can exist while their exact input scheduler rows are still
  `BLOCKED` (process death before `complete_barrier`). Restore must validate
  the durable receipt and publish its READY/PENDING_SINK successor in the same
  strict barrier completion that consumes those inputs. Never replay the
  committed plugin/merge, and never let completed-key reconciliation discard
  the persisted result as if every blocked parent were a late arrival.

- **2026-08-12 — a long-running transform must re-prove scheduler ownership
  before terminal audit writes**: `TransformExecutor` calls the processor's
  rate-limited active-claim heartbeat immediately after plugin return or
  exception and before node-state completion, transform-error/routing writes,
  contract evolution, or result visibility. If recovery or eviction has moved
  authority, `NodeStateGuard.abandon_open_state()` leaves that stale attempt
  OPEN (the honest hard-kill image) and the ownership-loss exception must
  propagate immediately; do not auto-fail, complete, or otherwise mutate the
  stale attempt. The scheduler drain then clears any in-memory staged branch
  losses and records only the canonical lease-loss evidence.
- **2026-08-12 — sink-redrive recovery is admitted by the complete durable
  bundle, not by `pending_sink_name` alone**: a `LEASED` row with any
  sink-redrive field set is sink-shaped debt and must satisfy
  `pending_sink_bundle_clause()` before it can return to `PENDING_SINK`.
  Repeat the same subtype/bundle predicate inside the recovery CAS; the
  diagnostic SELECT is not the safety boundary. A partial or concurrently
  corrupted bundle fails closed and the whole recovery transaction rolls back
  without rotating attempts, changing owners, or appending events.

- **2026-08-11 — the AWS IAM policy templates and the deploy README's floor
  commit are both pinned; editing either without its sibling update is red**:
  `tests/unit/deployment/test_aws_iam_policy_oracles.py` now pins the exact set
  of actions granted under an `aws:RequestTag/ACCEPTANCE_RUN_ID` condition and
  the exact set of wildcard patterns, so any grant added to
  `deploy/aws-ecs/terraform/iam/*.json.tftpl` fails until it is adjudicated.
  The verdict a new create needs: does the API ALSO authorize against a
  pre-existing untagged parent (the D11 trap — `ec2:CreateSubnet` also
  authorizes against its VPC, which carries no request tag)? If yes, add the
  `aws:ResourceTag` arm to the policy AND record it in
  `_DUAL_PURPOSE_PARENT_ARMS`; recording it WITHOUT the arm is red, because
  every entry is proved against the rendered policy — an earlier revision of
  this gate let a novel Sid discharge the pin with no arm present, which was
  worse than not gating at all, since the green then asserted a verdict had
  been reviewed. Neither set decides whether an API is dual-purpose (a fact
  about AWS, not about this tree); they only make the question unskippable.
  Two boundaries worth knowing: create-shaped actions granted OUTSIDE a
  RequestTag condition are adjudicated by nothing, and membership pins the
  action SET, not the condition SHAPE.
- **2026-08-11 — "Minimum image revision" in `deploy/aws-ecs/terraform/README.md`
  is machine-checked, not prose**: ship a new `ELSPETH_WEB__` name and
  `test_documented_minimum_image_revision_is_the_true_settings_floor` fails
  until that paragraph names the earliest ancestor of HEAD whose `WebSettings`
  defines every shipped name. Correct the paragraph, never the number alone.
  It was last wrong by six settings — `settings_from_env` raises on an unknown
  key and `WebSettings` is `extra="forbid"`, so an operator obeying it pins an
  image that fails every task at settings load, after a successful apply. The
  test skips only when the checkout has no git history at all, and fails
  loudly under `GITHUB_ACTIONS` (same rule as `_require_terraform`,
  elspeth-af1efcb8d8); an unresolvable or non-ancestor SHA is always red.
- **2026-08-11 — cancellation-safe settlement outcomes belong in the locked
  transaction**: a deferred-cancellation wrapper drains its shielded database
  worker, then deliberately re-raises `CancelledError`. Any audit write left
  to an outer exception handler can therefore be skipped even though an
  earlier dispatch committed successfully; process failure creates the same
  gap. For commit-boundary trust revocation, insert or exactly reuse
  `auto_commit.revoked` inside the session-locked settlement transaction,
  return the revocation as an internal outcome so the context commits, and
  raise `TrustModeAutoCommitRevokedError` only after `_run_sync` returns. The
  route translates that error but never owns a second revocation write.

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
