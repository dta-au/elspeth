# Recent code hints — READ BEFORE WRITING CODE

**Audience: agents. This is a rolling document.** It exists because agents keep
landing commits that pass their scoped test run and then break whole-tree
gates for every sibling on the branch (most recently 7201beeb7 →
elspeth-62a5aa4da8). Each entry is dated; when you land a new convention or a
new whole-tree trap, ADD IT HERE in the same commit. Prune entries once they
are covered by permanent docs or no longer bite. No sign-off ceremony — this
is a working document under the normal delivery posture.

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
