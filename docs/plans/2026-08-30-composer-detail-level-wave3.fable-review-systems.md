> Saved by team-lead from the reviewer's inline return (its file write was harness-blocked). Truncated mid-I-1 at "so every user gets the" — remainder requested via SendMessage; PART 2 appended below when received.

**VERDICT: APPROVE_WITH_CONCERNS**

**Counts:** 0 Critical · 4 Important · 4 Minor · 3 Observations. No composer-invariant violations.

**Important findings (one line each):**
- **I-1** `preferencesDecoder.ts` KEYS ↔ `web/preferences/models.py` `ComposerPreferences` has NO parity gate: a backend field addition ships green CI, then the exact-record decoder rejects every GET at runtime — preferences banner for all users, `defaultMode: null`, forced-freeform session creation, `show_advanced` stuck off. Remedy: the wave's own `test_graph_topology_parity.py` pattern, ~30 lines.
- **I-2** `graphTopology.ts` is not yet sole authority: `GraphView.tsx:128 buildProducerRegistry` keeps a full shadow copy of the four producer-registration rules, guarded only by a single fixture-scoped equivalence test — a new publication *field/rule* (vs. a set member) diverges silently unless `SHARED_FANIN_FIXTURE` is remembered; Graph vs Spec tab topology can drift again.
- **I-3** Egress dual-register (`ExecuteButton.tsx`) builds the summary twice and throws on length mismatch inside the run-confirm render path — a future register-gated sentence crashes the consent dialog at runtime; a single walk emitting `{text, identifiers}` pairs makes misalignment unrepresentable and deletes the guard.
- **I-4** The default-DOM pin's `SNAKE_RE` admits no digits, and the wave curated digit-free fixtures around it (self-documented in two tests) — the gate is systematically blind to production-shaped ids (`llm_2`, `invest_cs1_done`); widen the regex instead.

**Minor:** unknown-enum register contradiction between `diagnosticPhrases` (`<code>`) and `specRouting` (title-cased fake prose; reachable via the open `scope_policy` map); unknown audit-characteristic flags now render *nothing* — a silent under-disclosure window under bundle/backend skew on an audit surface; two `.import-yaml-actions` pin exemptions use the discouraged subtree (`closest`) form; committed `test.skip` Playwright negative controls will rot silently.

---

# Full report — Systems Review, Composer Detail Level Wave 3 (7cd2fc6db..8b85a9314)

Read the full 7,179-line diff package plus live worktree files (`RunsHistoryDrawer.tsx`, `preferencesStore.ts`, `web/preferences/models.py`, `GraphView.tsx`, `pluginDisplayName.ts`).

## Verdict: APPROVE_WITH_CONCERNS

No invariant violations, no blocking defects. Four Important findings, all of the "this will bite in six months" class rather than "broken today"; four Minor; three observations. None requires re-execution before merge, but I-1 and I-4 deserve follow-up tickets before the next wave builds on these mechanisms.

## Important findings

### I-1. The preferences exact-record decoder is the only closed cross-language contract in this wave WITHOUT a parity gate

- **Where:** `src/elspeth/web/frontend/src/api/preferencesDecoder.ts` (`KEYS`, 10 members) vs `src/elspeth/web/preferences/models.py` (`ComposerPreferences`, 10 fields — verified they match today).
- **Mechanism:** `exactRecord` rejects both a missing and an *unexpected* key, fail-closed. The backend's lockstep covenant (models.py comments) names three Python sites (Literal, DB CHECK, Tier-1 read guard) — the TS decoder is the un-named fourth, as its own header admits ("this decoder would otherwise become a silent fourth"). Nothing executable ties `KEYS` to `ComposerPreferences.model_fields`.
- **Failure scenario:** a developer adds an 11th field to models.py + CHECK + service (following the covenant as written), runs the full pytest suite (green — no Python test reads the TS decoder) and vitest (green — `preferencesDecoder.test.ts` uses its own local `full` fixture). Ships. At runtime the decoder throws `unexpected <key>` on every GET; `bootstrap()`'s catch (verified at `preferencesStore.ts:162-222`) leaves `defaultMode: null`, so every user gets the
[TRUNCATED — awaiting remainder]

---
## PART 2 (resent remainder; continues mid-sentence from "so every user gets the")

"Couldn't load your preferences" alert, `resolveDefaultMode` throws, session creation degrades to forced-freeform, tutorial resume is lost, and `show_advanced` is pinned false with no way to enable it. Green CI; detection point is a user report. Note the failure does **not** require deploy skew — an atomic deploy whose frontend simply wasn't updated triggers it (a backend dev following the covenant as written never touches the frontend, so the shipped bundle carries the old decoder).
- **Systemic irony:** this same wave built exactly the right tool (`test_graph_topology_parity.py`: regex the TS literal, compare against the Python authority, single-match + vacuity guards) and left the highest-churn closed contract (Wave 3 itself added `show_advanced` as the 9th key) comment-guarded. The decoder's own header admits the covenant gap: "this decoder would otherwise become a silent fourth" — the models.py covenant names three Python sites and not the decoder.
- **Remedy:** add a parity test comparing `set(ComposerPreferences.model_fields)` against the `KEYS` tuple parsed from `preferencesDecoder.ts` (identical pattern to `test_coalesce_member_tuples_match_the_backend_literals`), and add the decoder to the models.py covenant list. ~30 lines, pattern already in-tree.

### I-2. `lib/graphTopology.ts` is not yet the sole authority it claims to be

- **Where:** `src/elspeth/web/frontend/src/components/inspector/GraphView.tsx:128` (`buildProducerRegistry`, still used at :1231) vs `src/elspeth/web/frontend/src/lib/graphTopology.ts` (`buildConnectionProducers`); cross-check in `src/elspeth/web/frontend/src/lib/graphTopology.test.ts` (`SHARED_FANIN_FIXTURE`).
- **Mechanism:** the lift moved `publishedSuccessConnection`, `branchEntries`, `FAN_IN_NODE_TYPES` and the member tuples — but the four producer-registration loops (source on_success; node published/on_error/routes; gate fork_to) now exist TWICE, and the test comment records the non-merge as deliberate ("the two implementations that this task deliberately did NOT merge"). Equivalence is guarded by ONE shared fixture, modulo the two sentinels. The consumer-side rule ("branches when non-empty, else input") is likewise restated in `specRouting.nodeInputs` and in GraphView's phase-1 `aliasMappedFanInIds` guard — shared *membership*, duplicated *rule*.
- **Loud-or-silent (the brief's question):** a new self-publishing or fan-in node *kind* fails loudly — the Python parity test compares the sets. A new publication *field* (say an `on_timeout` lane) added to one copy and not the other fails silently unless `SHARED_FANIN_FIXTURE` is also extended to exercise it — reproducing the exact Graph-tab-vs-Spec-tab divergence the module header vows to prevent.
- **Remedy (either):** (a) finish the lift — have GraphView decorate `buildConnectionProducers` output with its edgeType/label instead of re-walking the composition (the decoration is derivable: published→success, on_error→error, routes→route label, fork→branch name); or (b) keep the twin but make the cross-check assert that the fixture exercises every registration arm (e.g. count distinct arm kinds registered), so an un-exercised new arm trips the vacuity guard instead of passing it.

### I-3. The dual-pass egress summary converts a future maintenance slip into a runtime crash on the run-confirm dialog

- **Where:** `src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.tsx` — `egressSentences(register, …)` called twice; `buildRunEgressSummary` throws `"egress registers disagreed on line count"`.
- **Mechanism:** the alignment property ("register substitutes label TEXT, never gates emission") is enforced by a runtime throw inside the Run-button render path rather than by construction. The guard re-checks a convention instead of deriving from the structure that produces the data.
- **Failure scenario:** a later edit adds a reader-only clarifying sentence under `if (register === "reader")` — precisely the shape of edit this function's struc
[TRUNCATED — awaiting part 3]

---
## PART 3 (continues mid-sentence from "the shape of edit this function's struc")

ture invites, since the whole body is `register ===` conditionals. If the branch is data-dependent (only for LLM sources, say), unit fixtures miss it and the RUN dialog throws in production for exactly the users trying to run that pipeline shape. Loud, yes — but loud on the consent surface of the product.
- **Remedy:** single walk pushing `{text, identifiers}` pairs at each emission site. Misalignment becomes unrepresentable; the throw and the double traversal both delete, and the "aligned by construction" comment becomes true instead of asserted. This is the project's own doctrine ("stop parsing, carry the fact structurally"; "a guard must derive from its authority") applied to this function.

### I-4. The default-DOM pin's SNAKE_RE is digit-blind, and the wave curated fixtures around the blindness instead of fixing it

- **Where:** `src/elspeth/web/frontend/src/test/defaultDomPins.ts` — `SNAKE_RE = /\b[a-z]+_[a-z_]+\b/`; workarounds self-documented in `PipelineSpecView.test.tsx` ("Digit-FREE identifiers: SNAKE_RE … admits no digits, so `invest_cs1_done` would produce a pin that cannot fail") and `AuditCharacteristicIcon.test.tsx` ("the digit-free form is what the rest of this wave's fixtures use").
- **Mechanism:** the ONE executable form of the wave's acceptance rule cannot see `llm_2`, `step_1_extract`, `invest_cs1_done` — the auto-suffixed id shapes the composer's planner actually generates for duplicate plugins. Fixture curation keeps the committed tests meaningful, but every future good-faith `expectNoIdentifiersInDefaultDom` call is a false certificate for the most common production id shape.
- **Failure scenario:** a future surface leaks `classify_2` into visible text; its component test calls the pin in good faith and passes green. (The repo has paid for this regex class before — the count regex that dropped 59 files by not admitting digits in filenames, now standing doctrine.)
- **Remedy:** widen to e.g. `/\b[a-z][a-z0-9]*_[a-z0-9_]*[a-z0-9]\b/`, re-run the frontend suite, fix what lights up. Prose false-positive risk is low (English prose does not contain underscores); the UUID/HEX32 REs already own the hash shapes.

## Minor findings

- **M-A. Two contradictory precedents for "unknown enum value" in one wave.** `diagnosticPhrases.ts` rules an unphrased value "is never dressed up as a sentence" and renders `<code>`; `specRouting.routingPhrase` title-cases an out-of-union policy/merge/scope_policy into prose-looking text (`"someday_maybe"` → "Someday Maybe", pinned by its own test). For policy/merge the fallback is nearly unreachable (parity test covers backend adds, the closed Records cover frontend adds), but `scope_policy` is explicitly OPEN (no backend Literal; `string | null` in types/index.ts), so its unknown arm is reachable and will render fake prose. The next maintainer has two in-repo archetypes to copy and no rule for choosing. Suggest a one-line register-doctrine note in specRouting, or aligning the open-map arm on the `<code>` register like `DiagnosticValue`.

- **M-B. Unknown audit-characteristic flags now render NOTHING — an under-disclosure window under version skew, on an audit surface.** `AuditCharacteristicIcon` plus the PluginCard lookup filter delete the raw-flag fallback chip; the vocabulary parity test is the drift guard. That guard runs in CI, not at runtime: a stale cached bundle against an updated backend (or any path where a new backend flag reaches an old frontend) now silently hides an audit characteristic that the old grey chip would have disclosed raw — the under-disclosure class R2-F7 fought. The commit rationale ("a fallback chip could only ever have shown a raw flag after the gate was already red") assumes CI-gated atomic delivery; the browser cache sits outside that assumption. Accepted tradeoff per elspeth-0bfd019f68 — recording the residual, not contesting the ticket.

- **M-C. The two `.import-yaml-actions` pin exemptions use the discouraged subtree (`closest`) form.** `CatalogD
[part 4 received — report complete]

---
## PART 4 (final; continues mid-sentence from "`CatalogD")

rawer.test.tsx` and `ImportYamlModal.test.tsx` exempt a container whose future children are silently exempt too — the exact hazard `defaultDomPins.ts`'s own docs warn about ("silently exempts every label added inside that subtree later") and the new self-only matcher was built to avoid. Legitimate today (the labels genuinely sit on child buttons), but these two calls are now the in-repo copy-paste source for the lazy form. Tightening to self-selectors on the buttons themselves closes the growth channel.

- **M-D. Committed-but-skipped Playwright negative controls will rot silently.** `tests/e2e/composer-workspace-graph-keyboard.spec.ts` carries two `test.skip` blocks whose comments assert hand-verified failure behavior dated 2026-08-30. They never run in CI or a normal `npx playwright test` invocation, so selector/fixture drift will invalidate the documented manual procedure with no signal while the comments continue to claim verification. Acceptable as a documented procedure; flagging the staleness mechanism.

## Observations (no action required)

- **`title` is the sanctioned raw-id channel and is deliberately un-pinned — but `title` participates in the accessible-description computation**, so at the default detail level identifiers DO reach screen readers as descriptions on `<dd>`s, chips, and headings. Coherent with the wave's rulings (the raw form stays reachable), but the boundary — "identifiers may appear in accessible descriptions, never in accessible names or visible text" — is nowhere stated or tested as such. If a future ticket treats title-borne ids as a leak, the pin will need a third scan loop.
- `ComponentReviewTurn` renders `item.name` raw in `<strong>` and in the listitem aria-label, while the Spec tab title-cases the same class of author-chosen names — a small cross-surface register inconsistency for snake_case guided source names.
- `buildProducerRegistry` is exported from `GraphView.tsx` solely for the cross-check test — a production component exporting a test-only symbol; it folds away if I-2 remedy (a) is taken.

## Confidence and Information Gaps

**Confidence: high** on everything derived from the diff and live files — I read the full 7,179-line diff package plus `RunsHistoryDrawer.tsx` (confirmed the failure row's raw-id fallback lands in `<code>`, consistent with doctrine), `preferencesStore.ts` (bootstrap catch verified at :162-222: `defaultMode` stays null, `loaded: true`, `writeError` set), `web/preferences/models.py` (field list verified to match the decoder's `KEYS` today; covenant comment names three Python sites only), `GraphView.tsx` (confirmed `buildProducerRegistry` defined at :128 and consumed at :1231), and `pluginDisplayName.ts`.

**Medium confidence** on the deploy-skew specifics in M-B: I did not inspect container packaging to confirm frontend and backend ship atomically. Note that I-1 does **not** depend on skew — it fires on an atomic deploy whose frontend simply wasn't updated alongside a backend field addition, which no current test can detect.

**Not verified:** I did not run the test suites (the executor reports gates green at 8b85a9314, with the two known pre-existing pytest failures in `tests/unit/scripts/cicd/test_runtime_rejection_parity_gate.py`); I did not audit GraphView's node-label rendering beyond the diff (the Graph tab is an identifier surface by plan). M2 (`listSecrets` unchecked `as T` cast, `api/client.ts:1640`) and M6 (`ValidationResult.tsx:81` renders `${node_type}:${id}` in prose) were excluded as already ticketed per the brief.

END OF REPORT
