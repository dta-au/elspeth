# Composer Detail Level (Wave 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the epic's register residue and cleanup rows — the step-label fallback reversal and the Spec tab's remaining raw ids, the eight-item register batch, the freeform brief's reader-register rule, the graph-list keyboard e2e, three minor `show_advanced` gates, and the dead unknown-characteristic chip — plus the small mechanism and hygiene items Wave 2 deferred.

**Architecture:** No new mechanisms and no new backend surface. Every visible identifier goes through an existing single-authority helper — `stepLabelForNodeId` / `humaniseStepLabel` (`chat/interpretationStepLabel.ts`), `pluginDisplayName` / `titleCaseLabel` (`catalog/pluginDisplayName.ts`), `makePhraseFor` (`lib/validationHumaniser.ts`) — with the raw id demoted to `title`. Connection topology is likewise a single authority, and it already exists: `inspector/GraphView.tsx` carries the backend-mirrored rules (`publishedSuccessConnection`, `branchEntries`, the fan-in and implicit-publisher kind sets) as module-private helpers. Task 3 LIFTS them into `lib/graphTopology.ts` unchanged — a LEAF module in the repo's own home for one-rule-one-place helpers (`lib/validationHumaniser.ts`, and `chat/guided/stepLabels.ts`, which exists because "three hand-mirrored STEP_LABELS copies had drifted") — together with `GraphView.tsx`'s producer-registration rule (reduced to `buildConnectionProducers`, without its ReactFlow decoration), the two coalesce member sets `api/guidedDecoder.ts` held privately, and the routing-value `discard` sentinel that two production sites spell by hand. Task 3 also re-anchors the Python↔TypeScript parity gate that reads those declarations off disk — the wave's only backend touch outside Task 6. Task 4's new `workspace/specRouting.ts` builds on all of it, so the Spec tab can say "Then → Extract Invoice" instead of `raw_rows` without re-deriving a rival model of the graph. Gated surfaces read `useShowAdvanced()` and simply omit the technical control with the flag off (the Wave 2 `CompletionBar` precedent: the sibling content IS the plain summary; no "available at the detailed level" hints). The planner-brief change is skill text only (Composer invariant 1; ADR-031). The shared default-DOM pin helper gains an `allowAriaLabelSelectors` option so surfaces whose accessible names carry author-chosen ids by design can join the gate.

**Tech Stack:** React 18 + Zustand + vitest/@testing-library + Playwright (frontend, `src/elspeth/web/frontend`), pytest (backend, `tests/unit/web/composer`), FastAPI (unchanged).

**Spec:** Wave 2 plan §"Roadmap: Wave 3" (`docs/plans/2026-08-30-composer-detail-level-wave2.md:2299-2316`) — the six-row table is the binding scope; the **Wave 1 epic comment on `elspeth-cd8abcba3f`** (`filigree get-comments elspeth-cd8abcba3f`, `sdd-controller`, 2026-08-29T15:47:55Z) is the Spec-tab acceptance baseline, and it is the ONLY retrievable one: **40 residual register items post-`3b7281965` (12 node-id headings, 23 branch/wire-stage values, 5 policy/plugin vocab), plus a further 37 that are reader data (column names, prompt text) — verbatim by design and OUT OF SCOPE.** A pre-fix draft of this line cited `.superpowers/sdd/2026-08-29-composer-detail-level-wave1/live-check-report.md` and the figure **83**. That path does not exist, `.superpowers/` is gitignored (`.gitignore:37`) so no revision can recover it, and 83 was the *pre-fix* raw count taken before `3b7281965` and `900b86b8f` landed. Sizing Task 4 against 83 would send a lane at the 37 authored reader-data items the Wave 2 roadmap (`docs/plans/2026-08-30-composer-detail-level-wave2.md:2314`) explicitly flags as possibly-correct-as-is — forbidden work, not merely overcounting. **Task 4 and Task 11 Check 1 both scope against 40/12/23/5, never 83.** Epic `elspeth-cd8abcba3f`; tickets `elspeth-93f5621f18`, `elspeth-d74ab492dd`, `elspeth-4bf65fe149`, `elspeth-d1feee1e67`, `elspeth-f1394307e3`, `elspeth-0bfd019f68`; absorbed follow-ups `elspeth-13b69b5846`, `elspeth-59631ec7f7`, `elspeth-7d07df6438`. Every `file:line` below was re-verified against branch head `cde8a279b` (2026-08-30) — see the drift record immediately below for the current base and why `cde8a279b`'s citations still hold against it; the ticket bodies carry 2026-08-28 line numbers that have drifted and are NOT authoritative.

**Base-commit drift — READ THE RULE, NOT THE SHA.** The branch has moved under this document on every pass, so no sha written here is "current" and this paragraph deliberately does not claim one. The rule: **the citations in this plan were verified at `c601c957b`** (the round-2 review baseline; an earlier pass verified them at `cde8a279b`, an ancestor). Every commit between `cde8a279b` and `c601c957b` was checked against this plan's citation set and none of them touches a file this plan cites — the itemisation is below. `c601c957b` `fix: restore documentation and plugin hash gates` carries `src/elspeth/plugins/transforms/azure/document_intelligence.py` and `src/elspeth/plugins/transforms/llm/transform.py`: **those two files are BASE STATE, expected to appear in `git log`, and are not the out-of-scope plugin churn the Global Constraint tells you to stop for.** `c601c957b` carried no `tests/golden/**` file, so the goldens are consistent with it. At the time this fix pass ran, HEAD had already moved one further commit to `7d0fdf534` `docs(agents): scrub retired-Wardline guidance from live agent docs` — docs-only (`docs/agents/recent-code-hints.md`, `docs/plans/2026-08-29-str-any-mapping-burndown.md`), no source files, no citation affected. **Do not "refresh" this paragraph to whatever HEAD you find; Task 0 Step 1 records the real branch point beside the corpus count, and that capture is the authority.** The intervening commits, all checked against this plan's citation set:

- `dffa61b7a`, `cac5a8ccb`, `7a12fe3f0` — docs-only (a tier-burndown sweep restore, and the design + plan for the composer-preferences OK action). No source files.
- `0411c438c` `feat(web): add Composer preferences OK action` — `settings/ComposerPreferencesPanel.tsx`, `settings/ComposerPreferencesPanel.test.tsx`, `settings/settings.css`, `settings/settingsSurface.test.ts`.
- `8392d0113` `feat(lints): check-judge-quality can measure the judge that actually signs` — `elspeth-lints/…/cli.py`, `judge_quality.py`, `tests/unit/elspeth_lints/test_judge_quality.py`.
- `c601c957b` `fix: restore documentation and plugin hash gates` — this plan file itself, plus `src/elspeth/plugins/transforms/azure/document_intelligence.py` and `src/elspeth/plugins/transforms/llm/transform.py`. **Base state; see the paragraph above.** No frontend file, no `tests/golden/**`, no file this plan cites.

**No file this plan cites appears in any of those source commits.** Checked mechanically: the only mentions of those four `settings/` files anywhere in this document are in the paragraph below, which names them precisely to record that Wave 3 does NOT touch them. So every `file:line` below holds at `c601c957b`, which is where round 2's reality reviewers re-verified them independently.

Two consequences worth stating rather than leaving implicit. (a) The composer-preferences OK lane has **landed**, so it is no longer a concurrent lane to coordinate with — it is part of the base. (b) `8392d0113` changes `elspeth-lints` CLI code, which is the tool Task 0 Step 1 runs. That is precisely why Task 0 re-captures the corpus at the real branch point instead of trusting the Wave 2 close figure of 2354 — a different count here is the tool moving, not a finding. Re-run Task 0 Step 1 at whatever HEAD the wave actually branches from; that capture, not any number written here, is the authority for Task 11's diff.

**Sibling lane, for the record (now merged, previously concurrent):** the composer-preferences OK lane, planned in `docs/superpowers/plans/2026-08-30-composer-preferences-ok-button.md` (that plan file is itself being removed from the tree by a separate housekeeping pass — it showed as an unstaged deletion during this fix pass, so do not expect to find it; the lane's code landed in `0411c438c`), touched `settings/ComposerPreferencesPanel.tsx`, `settings/settings.css`, `settings/ComposerPreferencesPanel.test.tsx` and `settings/settingsSurface.test.ts`. Wave 3's only `settings/` file is `SecretsPanel.tsx` (Task 5 Step 2), and it adds no class name and no CSS rule — so there was zero file overlap and there is no merge interaction to expect. If that lane is ever re-run or extended, the same independence holds.

## Global Constraints

- **Read `CONTRIBUTING.md` §"Whole-tree gates and conventions you will hit" before touching code.** No new `getattr`/`hasattr` anywhere (attribute-contracts + masquerade gates scan the whole tree, tests included). Owned types get direct attribute access; parse only genuine Tier-3 boundaries via the file's existing idioms (per ADR-032). **No task in this wave adds a Python probe. TWO tasks touch Python files: Task 3 (re-anchors and renames `tests/unit/web/composer/test_graph_view_self_publishing_parity.py` → `test_graph_topology_parity.py` — a path constant, a docstring, eight path references and six assertion messages, plus two added parity assertions) and Task 6 (a Markdown skill file plus one Python test).** A pre-fix draft of this line said "Task 6 is the only backend-touching task"; that was false and actively misdirecting — it would have pointed whoever triaged a red backend suite at Task 6's skill change rather than at Task 3's relocation, which is the thing that actually breaks a Python↔TS parity gate. See Task 3's blast-radius bullet.
- **Trust-tier lint corpus is fail-closed and must not grow.** Task 0 captures the before-corpus at the branch point, before any lane lands; Task 11 diffs the after-capture against it; the diff must add nothing. Capture is **stdout only** (`> file`, never `2>&1` — stderr adds 5 WARNING lines and corrupts the count). Baseline at 37e939bc3 was 2354; re-capture at the Wave 3 branch point rather than trusting that number. Never hand-edit a `judge_metadata_signature`; never shape code around signature churn.
- **Composer invariants:** no server-side authoring of pipeline structure; **no tutorial-special paths** (ADR-031). Task 6 changes the brief only — no rewriting, filtering, or scoring of model output anywhere on the server, and no tutorial-conditional prose.
- **Audit-required elements stay visible regardless of `show_advanced`:** AuthorityChip, Audit panel rows + Blocks-run/Advisory legend, Run-confirm egress lines (every sentence; R2-F7 must not reappear), tool-outcome ribbon prefixes (Applied / Looked up / Completed / Ran / Attempted (not applied) / Failed / Cancelled), acknowledgement cards, completion honesty gate, "Validation passed · N checks" headline, the accounting-corruption badge, the audit-closure verdict line and missing/duplicate-terminal integrity warnings, the run history Cancel affordance, the wire-stage blocker panel, every version remaining revertable, the curated per-state failure row in run history (`RunStateFailureDetail`), RecoveryDiff + Discard/Apply in the recovery panel, and the blob row's status dot, creator badge and four actions. This wave changes the WORDS on several of these; it never hides one.
- **Debug mode expands disclosures; it never adds surfaces.** Every item hidden when the flag is off has a plain summary in its place. For Task 8 the plain summary is the sibling content that already carries the fact (the diff, the preview, the auto-refreshing panel) — no hint text pointing at the preference.
- **`<details open={showAdvanced}>` is uncontrolled after the first user toggle** (Wave 1 idiom, `OptionRows.tsx:201`). No task makes a `<details>` controlled.
- **Every flag reader goes through `useShowAdvanced()`** (`@/stores/preferencesStore`); the preferences panel is the only direct store reader.
- **No task regenerates goldens or touches `src/elspeth/plugins/*`.** If `tests/golden/web/catalog/knob_schema/*.json` or `docs/architecture/dag/scenario-corpus/v1/manifest.yaml` shows dirty, stop: something out of scope changed.
- **New TSX class names need real stylesheet rules** — `src/styles/classNames.test.ts` is a whole-tree gate; the directory gates (`catalogClassNames.test.ts`, `executionClassNames.test.ts`) also assert their `RULE_LESS_BY_DESIGN` names are still applied somewhere. Task 9 REMOVES a rule-less name and must remove its allowlist entry in the same commit or the "keeps the rule-less allowlist honest" test fails.
- **Test files touching a Zustand store must reset it** — a top-level `beforeEach(() => resetStore(useXStore))` (from `@/test/store-helpers`) in every test file whose component becomes a flag reader in this wave **and in any test file this wave adds a store-MUTATING test to**. (The narrower "becomes a flag reader" scoping missed the second class; widened here so the next wave does not re-derive the boundary.) The files this wave turns into readers: `RecoveryPanel.test.tsx`, `BlobRow.test.tsx`, `AuditReadinessPanel.test.tsx` (Task 8). Task 9's second `PluginCard` test sets `showAdvanced` and relies on that file's existing reset — confirm it is there. Checked across every file this wave adds a test to, **only `SecretsPanel.test.tsx` lacks a reset**, and it is a pre-existing gap this wave does not worsen (Task 5 Step 2 records why). The reset is a numbered step, not a "verify".
- **Copy register:** sentence case, no internal identifiers in visible text; raw identifiers go in `title`/`data-*` or a `<code>`/mono secondary span. A `<details>` is NOT a firewall for regex-based DOM pins — its children still land in the text scan.
- **The per-PR default-DOM acceptance pin is ONE helper, used by every task that renders DOM:** `expectNoIdentifiersInDefaultDom(container, { allowSelectors?, allowAriaLabelSelectors? })` from `src/test/defaultDomPins.ts`. It joins text NODES (not `textContent`; fixed 56065e665) and, after Task 2, exempts the aria-labels of elements matching `allowAriaLabelSelectors` — before Task 2 it has NO aria-label exemption beyond the ToolCallInfo trigger. `title` attributes are never inspected. The reviewer runs these tests first.

  **Two properties of the helper that this wave must not get wrong, both stated because a pre-fix draft got each wrong once.** (1) **`SNAKE_RE` is `/\b[a-z]+_[a-z_]+\b/` — it admits NO DIGITS**, so `future_flag_2027`, `flag_2027` and any trailing-digit identifier do **not** match and a fixture using one produces a pin that cannot fail (Task 9). Every pin fixture in this wave uses a digit-free identifier. (2) **`allowAriaLabelSelectors` matches with `el.closest()`, so it exempts the matched element AND its whole subtree** — pass the tightest selector that covers the labels you mean; a container selector silently exempts every aria-label added inside it later (Task 2's ruling; Task 4's two exact selectors). Until Task 2 the helper has **no direct test of its own** — Task 2 Step 1 adds `src/test/defaultDomPins.test.ts` with **four** cases (three mutation kills), red before the options land, because an over-broad exemption would make six tasks' acceptance evidence simultaneously vacuous while reporting green.

  **Which tasks carry the pin, and the two honest exemptions.** Tasks 1, 2, 4, 5, 8 and 9 each add at least one call (Task 8 adds three — one per gated component). The exemptions, stated rather than left as a silent gap:
  - **Task 3 (topology extraction) renders no DOM.** It is a pure relocation of module-private symbols out of two files, plus a unit test and a re-anchored Python parity test; its acceptance is **three** things: `GraphView.test.tsx` and `guidedDecoder.test.ts` staying green and untouched with a `git diff` on each showing pure relocation, AND `pytest tests/unit/web/composer/test_graph_topology_parity.py` green after Step 2b re-anchors, renames and extends it. A DOM pin there would have nothing to render.
  - **Task 9's own component becomes empty**, so `expectNoIdentifiersInDefaultDom` on `AuditCharacteristicIcon`'s container would be a check that cannot fail. The pin therefore goes one level up, on `PluginCard` (`AuditCharacteristicIcon`'s only consumer, `PluginCard.tsx:199`), where a raw backend flag would actually have reached visible text.
  - Tasks 6 (planner brief, Markdown + pytest), 7 (Playwright e2e) and 10 (API decoder, no DOM) render no React DOM at all; their acceptance is the pytest pin, the Playwright assertions and the decoder unit tests respectively.
- **The one rule for author-chosen ids (ruling for `elspeth-59631ec7f7`, applied in Tasks 1, 4, 5):** in PROSE surfaces (acknowledgement cards, wire blockers, execution/run history, run-confirm egress lines, Spec-tab headings and routing) an author-chosen component id renders through `stepLabelForNodeId`/`makePhraseFor` — description → acronym-aware title case of the id → plugin gloss — with the raw id in `title`. In IDENTIFIER surfaces (the catalog/import "unavailable component" row, guided component-review rows, `<code>` fallbacks) the id IS the actionable name the user must match against their own YAML or guided input and renders raw, in the identifier register (`<code>`), never bare prose. Cost if wrong: a surface classed wrongly shows a title-cased name where a copyable id was needed (recoverable via `title`) or vice versa (a snake_case token in prose, caught by the pin).
- **Shared checkout:** stage only your own pathspecs; never `git restore`/`clean` files you did not stage; no `git stash` (hook-blocked). **This forbids in-place mutation of a tracked file as a test technique, including a `cp` backup/restore round-trip** — a sibling lane's uncommitted work sits in this tree, and any window in which a tracked file holds content nobody staged is the same hazard class as `git restore`, only narrower. Task 7's negative control therefore injects its override at runtime (Playwright `addStyleTag`), never by editing the stylesheet on disk. Full `pytest tests/` is a background job in a worktree — cap parallelism at `-n 12` when other agents are running. Never run two Playwright commands concurrently in one worktree (auth state is worktree-global); Playwright boots its own backend on :8451 (`playwright.config.ts:19`).
- Frontend commands run from `src/elspeth/web/frontend`: `npx vitest run <path>`; `npm run lint` (`package.json:17` — eslint over `src` plus the enumerated e2e globs; `tests/e2e/composer-workspace-*.spec.ts` IS in the list, which is why Task 7's spec is named to match it); typecheck with `npx tsc --noEmit -p tsconfig.app.json && npx tsc --noEmit -p tsconfig.test.json && npx tsc --noEmit -p tsconfig.e2e.json` — `npx tsc --noEmit -p .` is broken (TS6305, `elspeth-062c1d0b7f`, parked). Backend from repo root with `source .venv/bin/activate`.
- **Staging discipline: every task's `git add` must list every path in its Files block, and every commit is followed by `git status --short`.** Each task's `git add` below was swept against its own Files block at the round-2 fix pass, because several Files blocks were widened during review and three staging commands had fallen behind them (Task 3 gained the parity test and `SchemaFormTurn.tsx`; Task 9 gained `PluginCard.tsx`; Task 10 gained `preferencesStore.test.ts`). **If you widen a task's Files block, widen its `git add` in the same edit** — in a shared checkout an unstaged edit is not a smaller problem than a wrong one, and a `git mv` compounds it by staging the rename while leaving the content edits behind.
- **Sequencing rule:** one PR per ticket; each PR's default-DOM regression pin (the shared helper above) is the acceptance test the reviewer runs first. Task order is mechanism-first (Tasks 1–2), then the shared connection-topology extraction and the Spec-tab residue it unblocks (Tasks 3–4, the largest visible win), then the register batch (Task 5), then the brief, e2e, minors, cleanup, and the absorbed decoder bug (Tasks 6–10).

  **The real dependency graph, derived from the snippets rather than from prose:**

  - **Mechanism edges: `{2, 3} → 4`.** Task 4 consumes Task 3's `graphTopology.ts` (`buildConnectionProducers`, `branchEntries`, `FAN_IN_NODE_TYPES`, `DISCARD_CONNECTION`) AND Task 2's `allowAriaLabelSelectors` option — without Task 2 the exemption argument is a TypeScript error, not a soft failure, so Task 4 Step 3 repeats that dependency inline.
  - **`1 → 5` (FILE edge, not a mechanism edge).** See the shared-file ruling below.
  - **NOT an edge: `2 → 3`.** A pre-fix draft wrote the chain as `2 → 3 → 4`, which manufactures an edge Task 3 does not have — Task 3 renders no DOM and calls the pin helper nowhere, as its own exemption bullet says. Tasks 2 and 3 run in parallel, and Task 3 is the critical path to Task 4, the wave's largest visible win. Do not serialise it behind Task 2.
  - **NOT an edge: `2 → 8`.** A pre-fix draft claimed "Task 8 consumes Task 2's pin option". Checked against Task 8's own snippets: all three of its pins are `expectNoIdentifiersInDefaultDom(container)` with **no options object**, so the pre-Task-2 signature serves them and Tasks 2 and 8 may run in parallel. (Task 8 Step 1's BlobRow pin previously carried an "exempt by selector if the fixture trips the scan" escape hatch that *would* have needed the option; that hatch is removed — see Task 8's fixture ruling.)
  - **Independent: Tasks 6, 7, 9 and 10** — disjoint production files, verified by shared-file analysis across the whole wave. Task 5 is independent of Tasks 3–4 but shares a file with Task 1.
  - Task 5 (register batch) has NO Spec-tab half — Task 4's own ruling pulls the `<h4>`/`Kind`/policy-enum items of `elspeth-d74ab492dd` into Task 4's file and PR — so Task 5 must NOT be serialised behind Tasks 3–4. (A pre-fix draft of this line claimed "Task 4's Spec-tab half depends on Task 3"; it was wrong in both halves and is corrected here.)

  **Ruling — Tasks 1 and 5 both edit `chat/AcknowledgementCard.tsx`; land Task 1 FIRST, or give both edits to one lane.** This is the only production-file collision in the wave (checked mechanically across every task's Files block). Task 1 edits the card `<section>` at `:597-604`; Task 5 edits the amendment-cap warning at `:711-716`. The two regions are ~110 lines apart so git merges them cleanly — **the hazard is not merging, it is STAGING.** The Global Constraint says *stage only your own pathspecs*, and `git add …/AcknowledgementCard.tsx` from either lane stages whatever the other lane has written to that file. Two concurrent lanes cannot both honour that rule on one path, and this repo's own recorded incidents (a failed hook leaving files staged; a sibling's commit sweeping staged files) make it foreseeable rather than hypothetical. **Serialising is preferred**: Task 1's edit is four lines and lands early anyway; Task 5 then finds `data-affected-node-id` already on the `<section>` and must not remove it. If they must overlap, one lane owns the file and stages it hunk-by-hunk with `git add -p`, and says so in its commit message. Cost if wrong: one lane's uncommitted work lands inside the other's commit, which is the failure mode the Global Constraint exists to prevent.

  **Second shared authority, lower risk, recorded so it is not lost:** Task 5 adds `"gpt"` to `ACRONYMS` in `catalog/pluginDisplayName.ts`, and Tasks 1, 2 and 4 all consume `titleCaseLabel` from that module. Task 4 pins title-cased strings in `specRouting.test.ts` and `PipelineSpecView.test.tsx`; none of those pinned strings contains a `gpt` segment (checked), so the risk is currently nil — it is the word "independent" that was inaccurate, not the outcome. If Task 4's fixtures gain a `gpt`-containing id, land Task 5 first.
- **Ticket mechanics.** Tickets assigned to a lane identity must be closed with `--actor <assignee>`.
  - **Tasks (eight of the nine):** `open → closed` is a direct SOFT transition with no required fields (`filigree type-info task`), so `filigree close <id> --actor <lane>` works from `open` or `in_progress` with nothing else to supply.
  - **The bug (`elspeth-7d07df6438`, type `bug`, status `triage`) is not the same shape, and one of its transitions HARD-fails.** `filigree type-info bug` declares `verifying → closed [hard] (requires: fix_verification)`, and `filigree close` has **no** `--field`/`-f` option to supply it (`filigree close --help`: only `--reason --status --force --expected-assignee --commit --json --actor`). A close attempt without the field aborts with `Cannot transition 'verifying' -> 'closed' for type 'bug': missing required fields: fix_verification`. Two further fields are declared required at earlier states — `severity` at `confirmed`, `root_cause` at `fixing` — but those transitions are SOFT, so they warn rather than fail.

    **Ruling — set all three fields at the transition that declares them, not just the one that hard-fails.** The hard gate is `fix_verification`, but leaving `severity` and `root_cause` unset ships a ticket whose own template says they are required, and the warning is exactly the kind of noise that trains a reader to ignore warnings. `filigree update` takes `-f key=value` (repeatable), so each is one flag on a command the plan already runs. Cost if wrong: three extra flags on two commands. The concrete sequence is in Task 0 Step 2 (claim) and Task 10 Step 3 (verify).

---

## File Structure

| File | Responsibility |
|---|---|
| `src/elspeth/web/frontend/src/components/chat/interpretationStepLabel.ts` (modify) | `humaniseStepLabel` reversal: present-but-unlabelable → title-cased author name; absent from a loaded composition → "Removed"; unloaded composition → title-cased id; new `isComponentPresent` |
| `src/elspeth/web/frontend/src/components/chat/AcknowledgementCard.tsx` (modify — **Task 1; SAME FILE as the Task 5 row below**) | `data-affected-node-id` on the card `<section>` (`:597-604`, forensic home for the raw id) |
| `src/elspeth/web/frontend/src/components/execution/ValidationResult.tsx` (modify) | the `!nodes` raw-id fallback → `phraseFor` |
| `src/elspeth/web/frontend/src/test/defaultDomPins.ts` (modify) | `allowAriaLabelSelectors` option |
| `src/elspeth/web/frontend/src/components/catalog/UnavailableComponentRow.tsx` (modify) | component id in `<code>` (identifier register by design) |
| `src/elspeth/web/frontend/src/stores/preferencesStore.test.ts` (modify) | move the tutorial-resume test into its describe block (Wave 1 deferral) |
| `src/elspeth/web/frontend/src/lib/graphTopology.ts` (create) | THE frontend connection-topology model — a LEAF module beside `lib/validationHumaniser.ts`. Lifted verbatim out of `GraphView.tsx`: `publishedSuccessConnection`, `branchEntries`, `IMPLICIT_SELF_PUBLISHING_NODE_TYPES`, `FAN_IN_NODE_TYPES`; lifted out of `GraphView.tsx:1245-1260` + `:1298-1351` (non-contiguous; `:1262-1296` stays) with its ReactFlow decoration stripped: `buildConnectionProducers`; lifted out of `api/guidedDecoder.ts`: `COALESCE_POLICIES`, `COALESCE_MERGES` (as union-typed tuples); plus the newly named `DISCARD_CONNECTION` routing-value sentinel (two hand-spelled sites, NOT `ProposalEndpointKind`'s) |
| `tests/unit/web/composer/test_graph_view_self_publishing_parity.py` → `test_graph_topology_parity.py` (modify + rename) | Task 3: the Python↔TS parity gate reads the moved declarations off disk by hard-coded path — re-anchored to `lib/graphTopology.ts`, and extended to cover `FAN_IN_NODE_TYPES` and the two coalesce Literals, which had no gate at all |
| `src/elspeth/web/frontend/src/components/chat/guided/SchemaFormTurn.tsx` (modify) | Task 3: `:74`'s bare `"discard"` becomes `DISCARD_CONNECTION` — the second and last hand-spelled routing-value site |
| `src/elspeth/web/frontend/src/test/defaultDomPins.test.ts` (create) | Task 2: the first direct coverage the shared acceptance gate has ever had — three mutation-killing cases for `allowAriaLabelSelectors` |
| `src/elspeth/web/frontend/src/components/inspector/GraphView.tsx` (modify) | the four helpers above become imports; **no behaviour change** — `GraphView.test.tsx` is untouched and must stay green |
| `src/elspeth/web/frontend/src/api/guidedDecoder.ts` (modify) | `:75-76`'s two private coalesce member sets become imports; **no behaviour change** — `guidedDecoder.test.ts` is untouched and must stay green |
| `src/elspeth/web/frontend/src/components/workspace/specRouting.ts` (create) | pure: connection index (built on `graphTopology`), connection → component phrases, routing/policy enum phrases |
| `src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.tsx` (modify) | `<h4>` step label + `title`, `Kind` humanised, routing `<dd>` phrases with raw in `title`, policy enums |
| `src/elspeth/web/frontend/src/components/chat/modelDisplayName.ts` (create) | `modelDisplayName(modelId)` — leaf segment, hyphens to spaces, acronym-aware title case |
| `src/elspeth/web/frontend/src/components/catalog/pluginDisplayName.ts` (modify) | `"gpt"` joins the acronym set |
| `src/elspeth/web/frontend/src/components/chat/ModelChip.tsx` (modify) | display name visible, raw id in `title`, aria-label in the reader register |
| `src/elspeth/web/frontend/src/components/settings/SecretsPanel.tsx` (modify) | `ScopeBadge` label map (Yours / Deployment / Organisation), raw scope in `title` |
| `src/elspeth/web/frontend/src/components/chat/AcknowledgementCard.tsx` (modify — **Task 5; SAME FILE as the Task 1 row above. Land Task 1 first — see the shared-file ruling in Sequencing**) | amendment cap warning at `:711-716` in characters, exact figures in an `.sr-only` span |
| `src/elspeth/web/frontend/src/components/execution/diagnosticPhrases.ts` (create) | closed map of known reason/cause diagnostic enums → prose |
| `src/elspeth/web/frontend/src/components/execution/RunsHistoryDrawer.tsx` (modify) | curated failure row: known reason/cause as prose (raw in `title`), unknown stays `<code>` |
| `src/elspeth/web/frontend/src/components/execution/execution.css` (modify) | stale "inside the diagnostics disclosure" comment (`:147-149`) |
| `src/elspeth/web/frontend/src/components/audit/ExplainDialog.tsx` + `audit.css` (modify) | narrative through `MarkdownRenderer`, not `<pre>` |
| `src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.tsx` (modify) | `buildRunEgressSummary` returns `RunEgressLine[]` — reader `text` + identifier `identifiers` (the unchanged sentence, in `title`) |
| `src/elspeth/web/frontend/src/components/chat/InlineSourceCreatedTurn.tsx` (modify) | provenance label map, raw in `title` |
| `src/elspeth/web/frontend/src/components/chat/guided/ComponentReviewTurn.tsx` (modify) | drop the literal "reviewed" word; plugin by display name with raw in `title` |
| `src/elspeth/web/composer/skills/pipeline_composer.md` (modify) | new "Reply Register" section + termination checklist line |
| `tests/unit/web/composer/test_prompts.py` (modify) | brief-content pin for the reply-register rule |
| `src/elspeth/web/frontend/tests/e2e/composer-workspace-graph-keyboard.spec.ts` (create) | keyboard path through the graph a11y list |
| `src/elspeth/web/frontend/src/components/recovery/RecoveryPanel.tsx`, `blobs/BlobRow.tsx`, `audit/AuditReadinessPanel.tsx` (modify) | `show_advanced` gates |
| `src/elspeth/web/frontend/src/components/catalog/AuditCharacteristicIcon.tsx` + `catalogClassNames.test.ts` (modify) | delete the unknown chip and its rule-less allowlist entry |
| `src/elspeth/web/frontend/src/api/preferencesDecoder.ts` (create) + `api/client.ts` (modify) | structural decoder for the account-preferences payload (GET and PATCH) |

---

### Task 0: Preflight (hub, before any lane starts)

**Files:** none in the tree.

- [ ] **Step 1: Capture the lint corpus at the branch point**

**The baseline file is the sole authority for Task 11's fail-closed diff, and Task 6 reads it from a different agent process — so it goes somewhere durable and wave-namespaced, not a bare `/tmp` name.** A pre-fix draft wrote `/tmp/w3-lints-before.txt`, which does not survive a reboot, a container swap or a `/tmp` reaper, is not namespaced against a concurrent Wave 3 re-run, and had no recovery procedure at all.

```bash
source .venv/bin/activate
export W3_CORPUS_DIR="$HOME/.cache/elspeth/wave3-corpus"   # or the session scratchpad; NOT bare /tmp
mkdir -p "$W3_CORPUS_DIR"
git rev-parse HEAD | tee "$W3_CORPUS_DIR/branch-point.sha"   # record beside the count
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  elspeth-lints check --rules all --root src/elspeth > "$W3_CORPUS_DIR/before.txt"
grep -c . "$W3_CORPUS_DIR/before.txt"   # grep -c prints 0 and exits 1 on zero matches — do not wrap in set -e
```

**Capture is stdout ONLY (`>`), never `2>&1`:** measured at `c601c957b`, the run emits **2354 finding lines on stdout and 5 lines on stderr** (all copies of the shape-only-verification warning) with `rc=1`, so redirecting both streams corrupts the count and the diff. The `grep -c` caveat is real and separate: on a 2354-line file it exits 0, and it would exit 1 only on an empty corpus.

Record the count and the branch-point sha, and **broadcast `$W3_CORPUS_DIR` to every lane** — Task 6 Step 3 and Task 11 Step 2 both read it. The Wave 2 close figure was 2354 at `37e939bc3`; a different number here is branch drift or the `elspeth-lints` CLI moving (`8392d0113` changed it), not a finding — the before-capture is the authority for Task 11.

**Recovery, if the before-capture is missing when Task 11 runs.** Do NOT substitute the figure 2354 and do not skip the diff — the constraint is fail-closed and an unverifiable diff is a failed one. Re-create it from the recorded sha, in a worktree so the integration branch is not disturbed:

```bash
git worktree add /tmp/w3-baseline "$(cat "$W3_CORPUS_DIR/branch-point.sha")"   # or the sha from Task 0's record
ln -s "$(git rev-parse --show-toplevel)/.venv" /tmp/w3-baseline/.venv 2>/dev/null || true
(cd /tmp/w3-baseline && ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  elspeth-lints check --rules all --root src/elspeth) > "$W3_CORPUS_DIR/before.txt"
git worktree remove /tmp/w3-baseline
```

If even the sha is gone, the branch point is recoverable from the first Wave 3 commit's parent (`git log --oneline` on the integration branch); say in the Task 11 report that the baseline was reconstructed rather than captured, so the diff's provenance is on the record. **A narrower `--root` is not a substitute for a missing baseline:** `--root src/elspeth/web/preferences` crashes with `FileNotFoundError: .../contracts/declaration_contracts.py`.

- [ ] **Step 2: Confirm the tracker state**

**Snapshot, not a finding — re-verified at `c601c957b` (2026-08-30), and the branch has moved since:** all six Wave 3 tickets were `open`, type `task`, `Ready: YES (no blockers)`, parent `elspeth-cd8abcba3f`. Treat every state below as stale until the re-check in the next paragraph confirms it; do NOT skip that re-check because this paragraph looks authoritative. The absorbed follow-ups: `elspeth-13b69b5846` (task, open, no parent), `elspeth-59631ec7f7` (task, open, no parent), `elspeth-7d07df6438` (**bug, triage**, no parent). The hub re-runs `filigree show <id>` on all nine; if any has been claimed or closed since, stop and re-plan that task. Lanes then claim with `filigree start-work <id> --assignee <lane>`. Optionally parent the three absorbed tickets to the epic (`filigree update <id> --parent elspeth-cd8abcba3f`) so the epic's child list is the wave's ledger.

The bug is claimed differently — `--advance` walks it `triage → confirmed → fixing`, and those two states declare required fields (Global Constraints § Ticket mechanics). Supply them in the same call:

(The `filigree update` that sets `severity` and `root_cause` is the corrected block below — do not write the two fields twice.)

`severity` is an enum over `critical|major|minor|cosmetic`, default `major`. `fix_verification` is set later, at the `verifying` transition in Task 10 Step 3, because only then is there a verification to describe.

**The `root_cause` text above is corrected from a pre-fix draft that described a symptom which does not occur.** That draft said the defect was `undefined` reaching `<details open={undefined}>` and closing it "by accident". React renders `open={undefined}` and `open={false}` identically — the attribute is omitted, the `<details>` is closed — and the store's declared default IS `false` (`preferencesStore.ts:152`). Every `<details open={showAdvanced}>` consumer (`SchemaFormTurn.tsx:207,:230`; `WireStageTurn.tsx:436,:467,:510`; `ProgressView.tsx:108,:334`; `OptionRows.tsx:201`) and every `!showAdvanced` consumer (`ValidationResult.tsx:191,:210`) renders exactly as it does at the intended default. **No surface closes by accident, and committing an invented mechanism into a tracker field is the thing this project's no-fabrication doctrine forbids.** The real, distinguishable symptom is one line further down: `ComposerPreferencesPanel.tsx:178`, `checked={showAdvanced}` on the "Show technical detail" radio. `checked={undefined}` makes that input **uncontrolled** in React — it emits the "changing an uncontrolled input to be controlled" warning and the radio decouples from the store, while its `checked={!showAdvanced}` sibling at `:167` stays controlled. So the Detail-level control stops reflecting the preference: a genuine user-visible defect, and precisely what Task 11 Step 4 Check 5 exercises. Use this `root_cause`:

```bash
filigree update elspeth-7d07df6438 --actor <lane> \
  -f severity=major \
  -f root_cause="api/client.ts parseResponse ends in an unchecked response.json() as T cast, so a preferences payload with show_advanced omitted loads undefined into a boolean-declared store field. The distinguishable consumer is ComposerPreferencesPanel.tsx:178, checked={showAdvanced}: checked=undefined makes the 'Show technical detail' radio UNCONTROLLED, so it decouples from the store and stops reflecting the preference, while its checked={!showAdvanced} sibling at :167 stays controlled."
```

Severity stays `major`: an authoritative-looking control that silently stops reflecting the setting it names is a correctness defect on the wave's own preference surface, not cosmetic.

- [ ] **Step 3: Confirm the live-check baseline is retrievable**

`filigree get-comments elspeth-cd8abcba3f` and read the Wave 1 epic comment (`sdd-controller`, 2026-08-29T15:47:55Z) that records the post-`3b7281965` Spec-tab residual count for session 39578c6f. **The comment is present as of 2026-08-30 and records: "Residual snake_case baseline for Wave 2/3 = 40 (12 node-id headings, 23 branch/wire-stage values, 5 policy/plugin vocab); a further 37 are reader data (column names, prompt text) — verbatim by design."** Confirm it still says that. Task 11's Check 1 compares against **40**, and treats the 37 reader-data items as out of scope by design — never against the figure 83, which was the pre-fix raw count from a working artefact that no longer exists (see the Spec line at the top of this plan). Only if the comment has been deleted does Task 11 fall back to reporting an absolute count rather than a delta.

---

### Task 1: Step-label fallback reversal — no raw node id in prose (`elspeth-93f5621f18`, part A)

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/chat/interpretationStepLabel.ts` (doctrine comment `:120-124`, `stepLabelForNodeId` `:126-147`, `humaniseStepLabel` `:149-159`)
- Modify: `src/elspeth/web/frontend/src/components/chat/interpretationStepLabel.test.ts` (`:112-115` raw-id pin, `:250-254` fan_out pin)
- Modify: `src/elspeth/web/frontend/src/components/chat/AcknowledgementCard.tsx:597-604` (the card `<section>`)
- Modify: `src/elspeth/web/frontend/src/components/execution/ValidationResult.tsx:79` (`if (!nodes) return componentId;`)
- Test: `src/elspeth/web/frontend/src/components/chat/AcknowledgementStack.test.tsx`, `src/elspeth/web/frontend/src/components/execution/ValidationResult.test.tsx`

**Interfaces:**
- Produces: `humaniseStepLabel(state, nodeId): string` — never the raw id. `isComponentPresent(state, nodeId): boolean` (new export, used by `humaniseStepLabel` and, in Task 4, by `specRouting`'s `scope_opener` arm — see that task's ruling for why it is the ONLY routing field where "present vs absent" is a real distinction). `stepLabelForNodeId`'s null contract is UNCHANGED (ticket instruction). **Four DIRECT production callers depend on it** — `ReadinessRowDetail.tsx:64`, `SideRailValidationBanner.tsx:199`, `ValidationResult.tsx:118`, `PipelineValidationSummary.tsx:78` (all verified exact) — plus one INDIRECT dependant, the wire-blocker path at `ChatPanel.tsx:784`, which calls `humaniseStepLabel`, not `stepLabelForNodeId`. A pre-fix draft counted five direct callers; the distinction matters because the indirect one is affected by this task's change and the four direct ones are not.
- Callers of `humaniseStepLabel` (grep, 2026-08-30): `ChatPanel.tsx:784` (wire-blocker jump-link label via `acknowledgementCardTitle`) and `AcknowledgementStack.tsx:249` (card `stepLabel`). Neither reads the label back as an id — the card already holds `event.affected_node_id` — so the shared accessor changes and no caller gets its own (the ticket's "decide first" check, resolved: no round-trip).

**Ruling — the word is "Removed", not "a removed step":** every consumer composes the label into a template (`AcknowledgementCard.tsx:148,157,167`: `` `${stepLabel} step · prompt` ``), so the honest visible result is "Removed step · prompt". "a removed step" would render "a removed step step · prompt". The `source_data_contract` line (`:189`, "relies on certain columns from <em>{stepLabel}</em>") reads "from Removed." for a deleted source — an edge of an edge, left as is. Cost if wrong: one awkward sentence on a card for a source that no longer exists.

**Ruling — three fallback states, not one:** (a) composition not loaded (`state === null`) → `titleCaseLabel(nodeId)` — the best-effort author name, exactly what a loaded state yields for an author-chosen id, and no identifier leak; (b) composition loaded, component PRESENT but unlabelable (plugin-less structural node with no description) → `titleCaseLabel(nodeId)` for the same reason; (c) composition loaded, component ABSENT → "Removed". Before this task (b) and (c) both produced the raw id. Cost if wrong: (a) could title-case a plugin-derived id ("Llm 2") for the instant before the state loads — acceptable, it re-renders on load.

- [ ] **Step 1: Write the failing label tests**

In `interpretationStepLabel.test.ts`, replace the `:112-115` test and the `:250-254` test:

```ts
  it("names an absent node 'Removed' — never the raw id (elspeth-93f5621f18)", () => {
    const state = makeCompositionState([]);
    expect(humaniseStepLabel(state, "ghost_node")).toBe("Removed");
  });

  it("title-cases the id while the composition is still unloaded (unknown, not removed)", () => {
    expect(humaniseStepLabel(null, "extract_invoice")).toBe("Extract Invoice");
  });
```

and in the structural-node describe (`:250-254`):

```ts
  it("title-cases a present plugin-less node with no description (present, so not 'Removed')", () => {
    const state = makeCompositionState([gateNode()]);
    expect(stepLabelForNodeId(state, "fan_out")).toBeNull();
    expect(humaniseStepLabel(state, "fan_out")).toBe("Fan Out");
  });
```

Add to the same file (imports: add `isComponentPresent` to the import block `:12-18`):

```ts
describe("isComponentPresent", () => {
  it("finds nodes, sources and outputs; false for an absent id or unloaded state", () => {
    const state: CompositionState = {
      ...makeCompositionState([makeNode("rater", "llm")]),
      sources: { input: { plugin: "csv", options: {} } },
      outputs: [{ name: "results", plugin: "csv", options: {} }],
    };
    expect(isComponentPresent(state, "rater")).toBe(true);
    expect(isComponentPresent(state, "input")).toBe(true);
    expect(isComponentPresent(state, "results")).toBe(true);
    expect(isComponentPresent(state, "ghost")).toBe(false);
    expect(isComponentPresent(null, "rater")).toBe(false);
    expect(isComponentPresent(state, null)).toBe(false);
  });
});
```

(`makeCompositionState` / `makeNode` are the file's existing local factories at `:23-46`; check their exact shape before spreading — `sources`/`outputs` must match `CompositionState`.)

Run: `npx vitest run src/components/chat/interpretationStepLabel.test.ts` → FAIL (three failures: "ghost_node", "extract_invoice" raw, `isComponentPresent` undefined).

- [ ] **Step 2: Implement**

`interpretationStepLabel.ts` — replace the doctrine paragraph in the `stepLabelForNodeId` docblock (`:120-124`, "Returns null (not the raw id) … internal id must never leak into that prose.") with:

```ts
 * Returns null (not the raw id) on an unresolved id so callers can choose
 * their own "unknown" phrasing. No caller renders the raw id any more
 * (elspeth-93f5621f18): `humaniseStepLabel` below names an absent component
 * "Removed" and title-cases a present-but-unlabelable one; the
 * validationHumaniser callers (PipelineValidationSummary, ReadinessRowDetail,
 * ValidationResult) fall back to a generic phrase. The raw id lives in
 * `data-affected-node-id` / `title` for forensics, never in prose.
```

Add after `resolveNodePlugin`:

```ts
/**
 * True when `nodeId` names a node, source or output of a LOADED composition.
 * Distinguishes "absent" (the component was removed) from "present but
 * unlabelable" (a plugin-less structural node with no description) — the
 * two cases `humaniseStepLabel` words differently. Unloaded state is never
 * "present".
 */
export function isComponentPresent(
  state: CompositionState | null,
  nodeId: string | null,
): boolean {
  if (state === null || nodeId === null) return false;
  return (
    state.nodes.some((candidate) => candidate.id === nodeId) ||
    Object.prototype.hasOwnProperty.call(state.sources, nodeId) ||
    state.outputs.some((candidate) => candidate.name === nodeId)
  );
}
```

Replace `humaniseStepLabel` (`:149-159`):

```ts
/**
 * Humanised step label for an affected_node_id. NEVER the raw id
 * (elspeth-93f5621f18): an id the loaded composition no longer has reads
 * "Removed" (consumers append "step", giving "Removed step · prompt"); an
 * id the composition has but cannot label, or any id before the
 * composition has loaded, is title-cased as the author's own name — the same
 * result a loaded state gives an author-chosen id. "this step" only when
 * there is no id at all. See `stepLabelForNodeId` for the preference ladder.
 */
export function humaniseStepLabel(
  state: CompositionState | null,
  nodeId: string | null,
): string {
  if (nodeId === null) return "this step";
  const label = stepLabelForNodeId(state, nodeId);
  if (label !== null) return label;
  if (state !== null && !isComponentPresent(state, nodeId)) return "Removed";
  return titleCaseLabel(nodeId);
}
```

Run: `npx vitest run src/components/chat/interpretationStepLabel.test.ts` → PASS.

- [ ] **Step 3: Forensic home for the raw id on the card**

`AcknowledgementCard.tsx:597-604` — add one attribute to the `<section>`:

```tsx
    <section
      ref={sectionRef}
      id={acknowledgementCardDomId(event.id)}
      tabIndex={-1}
      className="ack-card"
      aria-labelledby={titleId}
      data-testid="acknowledgement-card"
      data-affected-node-id={event.affected_node_id ?? undefined}
    >
```

Add to `AcknowledgementStack.test.tsx` (it already renders a stack with a session store and `makeNode`-style fixtures — reuse its existing event/state factories; the describe's imports at `:10-34` already include `useSessionStore`, `resetStore`, `CompositionState`, `NodeSpec`):

```tsx
  it("names a deleted node 'Removed' in the card title and keeps the raw id on a data attribute (elspeth-93f5621f18)", () => {
    // An event whose affected node is absent from the loaded composition.
    useSessionStore.setState({ compositionState: makeCompositionState([]) } as never);
    seedPending([makeEvent("e-ghost", { kind: "llm_prompt_template", affected_node_id: "ghost_node" })]);
    const { container } = render(<AcknowledgementStack sessionId={SID} />);
    expect(screen.getByRole("heading", { name: "Removed step · prompt" })).toBeInTheDocument();
    const card = screen.getByTestId("acknowledgement-card");
    expect(card).toHaveAttribute("data-affected-node-id", "ghost_node");
    expect(card).not.toHaveTextContent("ghost_node");
    // The wave's acceptance gate, on the surface this task most changes: the
    // raw id survives ONLY on data-affected-node-id, which the pin does not
    // inspect (it reads text nodes and aria-labels).
    expectNoIdentifiersInDefaultDom(container);
  });
```

Imports for the new test: `expectNoIdentifiersInDefaultDom` from `@/test/defaultDomPins`. The factory names above are the file's REAL ones, verified 2026-08-30: `makeEvent(id: string, overrides: Partial<InterpretationEvent> = {})` — a positional id FIRST, then an overrides object, not a single object argument (`AcknowledgementStack.test.tsx:38-40`); `SID` (`:36`), not `SESSION_ID`; `seedPending` (`:95`). The composition-state factory is `makeCompositionState(nodes)` (the same local factory `interpretationStepLabel.test.ts` declares). Read `:36-130` and confirm before writing; do not add a parallel factory.

- [ ] **Step 4: The `!nodes` fallback in ValidationResult**

`ValidationResult.tsx:79` — `if (!nodes) return componentId;` becomes `if (!nodes) return phraseFor(componentId);`. Then retarget the docblock. The sentence that this change makes FALSE is mid-docblock at `:58-60`, not the docblock's last sentence (`resolveComponentName`'s docblock runs `:53-65` — `:52` is blank; its last sentence, about the `phraseFor` fallback for a vanished node, is already true and stays):

```
 * Absent that, with no nodes list at all there is nothing to resolve against
 * except the id itself — the caller passed no context, so the raw id is the
 * only honest thing to show.
```

becomes

```
 * Absent that, with no nodes list at all there is nothing to resolve against
 * except the shared plain-language resolver — which is the only honest thing
 * to show, because a bare raw id is not one (elspeth-93f5621f18).
```

Add to `ValidationResult.test.tsx` inside `describe("ValidationResultBanner detail level (elspeth-27efd1e801)")` (`:365`, which already resets the preferences and session stores):

```tsx
  it("never renders a bare component id when the banner has no nodes list (elspeth-93f5621f18)", () => {
    useSessionStore.setState({ compositionState: null });
    render(
      <ValidationResultBanner
        result={{ is_valid: false, errors: [{ message: "Field missing", component_id: "select_columns", component_type: "transform" }], warnings: [], checks: [] } as unknown as ValidationResult}
        onDismiss={() => {}}
      />,
    );
    expect(screen.queryByText(/select_columns/)).not.toBeInTheDocument();
    // getAllByText, not getByText: testing-library matches on each element's
    // own textContent, so a phrase inside an <li> also matches an ancestor and
    // a single-element query would throw on a multiple-match rather than on
    // the condition under test. The primary assertion is the queryByText
    // above — that the raw id is ABSENT — which is unambiguous either way.
    expect(screen.getAllByText(new RegExp(UNKNOWN_COMPONENT_PHRASE)).length).toBeGreaterThan(0);
  });
```

Imports: `import { UNKNOWN_COMPONENT_PHRASE } from "@/components/chat/guided/pipelineGloss";`. Match the banner's real props by reading how the existing tests at `:283` and `:319` render it (they pass `componentNames`; this test deliberately passes neither `nodes` nor `componentNames`). Run: `npx vitest run src/components/execution/ValidationResult.test.tsx src/components/chat/AcknowledgementStack.test.tsx src/components/chat/ChatPanel.test.tsx` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/frontend/src/components/chat/interpretationStepLabel.ts src/elspeth/web/frontend/src/components/chat/interpretationStepLabel.test.ts src/elspeth/web/frontend/src/components/chat/AcknowledgementCard.tsx src/elspeth/web/frontend/src/components/chat/AcknowledgementStack.test.tsx src/elspeth/web/frontend/src/components/execution/ValidationResult.tsx src/elspeth/web/frontend/src/components/execution/ValidationResult.test.tsx
git commit -m "feat(chat): humaniseStepLabel never renders the raw id — 'Removed' for a deleted node, author name otherwise (elspeth-93f5621f18)"
```

Ticket stays open until Task 4 lands (same ticket, part B).

---

### Task 2: Pin helper aria-label exemption + identifier-register catalog row + test hygiene (`elspeth-13b69b5846`; Wave 1/2 deferrals)

**Files:**
- Create: `src/elspeth/web/frontend/src/test/defaultDomPins.test.ts` — **the helper has NEVER had direct coverage** (`ls src/test` returns `composerFixtures.ts, defaultDomPins.ts, guided-fixtures.ts, inlineSourceIntegration.test.tsx, interpretationIntegration.test.tsx, node-fs.d.ts, playwrightConfig.test.ts, setup.ts, store-helpers.ts, a11y/` — no `defaultDomPins.test.ts`), and this task extends it
- Modify: `src/elspeth/web/frontend/src/test/defaultDomPins.ts` (`options` type `:37-40`, aria loop `:49-55`, closing `}` at `:56`; header `:1-10`)
- Modify: `src/elspeth/web/frontend/src/components/catalog/UnavailableComponentRow.tsx:45` (`<strong>{finding.component_id}</strong>`)
- Modify: `src/elspeth/web/frontend/src/components/catalog/CatalogDrawer.test.tsx` (add the pin)
- Modify: `src/elspeth/web/frontend/src/components/sidebar/ImportYamlModal.test.tsx` (add the pin)
- Modify: `src/elspeth/web/frontend/src/stores/preferencesStore.test.ts` (`:397-432` moves under `:868`)

**Interfaces:**
- Produces: `expectNoIdentifiersInDefaultDom(container, { allowSelectors?; allowAriaLabelSelectors?; allowAriaLabelSelfSelectors? })`. **TWO aria options with DIFFERENT matchers, and the difference is the whole point:**
  - `allowAriaLabelSelectors` matches with **`el.closest(selector)`** — it exempts the matched element **and its entire subtree**. Use it only when the labels you mean sit on CHILDREN of the selector. This task's `.import-yaml-actions` is the case: it is a container of two aria-labelled buttons (`CatalogDrawer.tsx:575,583`).
  - `allowAriaLabelSelfSelectors` matches with **`el.matches(selector)`** — it exempts **that element only**, and an aria-labelled descendant is still scanned. Use it whenever the label is on the element the selector names. Task 4's two selectors are this case: `article.pipeline-spec-card` (`PipelineSpecView.tsx:90-93`) and `div.option-rows` (`OptionRows.tsx:194`).

  Both skip the aria-label loop only; the text scan is unaffected by either.

**Ruling — a subtree exemption is a LAST RESORT, so the helper gains a SELF-ONLY matcher and the Spec tab uses it.** A first fix round replaced Task 4's `.pipeline-spec-card` with `article.pipeline-spec-card` and claimed that made it "two exact elements". **It does not.** `el.closest(selector)` walks up ancestors, and `closest` checks the element itself first — so for every aria-labelled descendant of a spec card, `el.closest("article.pipeline-spec-card")` returns the card. **Adding the tag qualifier narrows WHICH ELEMENTS MATCH THE SELECTOR, not WHICH ELEMENTS THE EXEMPTION REACHES.** The subtree breadth was unchanged and the ruling asserted a tightness the code did not deliver — which is the invisible-gate-erosion failure this ruling exists to prevent, committed inside the fix for it.

The Spec tab is the surface Task 4's pin is the acceptance test for, so "own the breadth with cost" is the wrong trade there: it would turn the aria half of the gate off, permanently, for the tab this wave exists to fix. Hence a second option rather than a wider one:

- **`allowAriaLabelSelfSelectors` (new, `matches()`)** — for labels ON the named element. `article.pipeline-spec-card` and `div.option-rows` both are.
- **`allowAriaLabelSelectors` (existing, `closest()`)** — for labels on CHILDREN of the named element. `.import-yaml-actions` genuinely is, and keeps it.

Three consequences the plan holds itself to:

- **Task 2's own subtree selector is `.import-yaml-actions`, NOT `.validation-banner-error-item`** (`UnavailableComponentRow.tsx:43`), which would exempt the row's own content too. Even a justified subtree exemption goes at the tightest ancestor.
- **Task 4 passes NEITHER form of `.pipeline-spec-card` to the subtree option.** It passes `allowAriaLabelSelfSelectors: ["article.pipeline-spec-card", "div.option-rows"]`, so a control added inside a spec card in future is SCANNED rather than silently exempted. See Task 4 Step 3.
- **The self-only matcher is itself tested** — Step 1 case 4 asserts that an aria-labelled DESCENDANT of a self-only-exempted element still fails. Shipping new exemption semantics without a mutation case would be this task's own BLOCKER one option later.

The helper's header records both matchers so a future caller does not re-derive them (Step 2).

**Ruling — the unavailable row's component id is an identifier surface:** the Remove/Replace buttons name the component by id in their aria-labels by design (`CatalogDrawer.tsx:575,583`), because the id is what the user must match against the YAML they imported. Per the one-rule constraint it renders in `<code>` (the pin's identifier surface) instead of bare `<strong>`; the plugin is already a display name with the raw id in `title`. This closes the catalog/import half of `elspeth-59631ec7f7`. Cost if wrong: a `<code>` where a bold name was wanted — a one-line revert.

- [ ] **Step 1: Write the failing self-test for the gate itself (RED, before the helper changes)**

**This is the one place the plan's red-first discipline was skipped, and it is the worst place to skip it.** `src/test/defaultDomPins.ts` designates this helper as "the acceptance test the reviewer runs first" for Tasks 1, 2, 4, 5, 8 and 9 — and the helper has no test file at all. An over-broad exemption would make six tasks' acceptance evidence simultaneously vacuous while every suite reports green.

The mutations this must kill, stated so the test is written to kill them rather than to pass:

| Mutation | Survives today's coverage? |
|---|---|
| `if (ariaExempt.length > 0) continue;` — hoist the `continue` out of the per-element `closest()` guard, disarming the whole aria loop whenever ANY exemption is passed | **Yes.** Tasks 2 and 4 pass an array (loop skipped, their pins still pass); Tasks 1, 5, 8 and 9 pass none (guard false, loop runs). Nothing distinguishes them. |
| `allowAriaLabelSelectors ?? ["*"]` | **Yes**, same reason. |
| letting the exemption leak into the TEXT scan (adding it to the `allowSelectors` spread) | **Yes.** The contract sentence "the text scan is unaffected" is asserted nowhere. |

Create `src/test/defaultDomPins.test.ts`. **Four cases**; case 1 alone is not enough — cases 2, 3 and 4 are the mutation kills, and case 4 is the only thing standing behind the new self-only matcher:

```ts
import { describe, expect, it } from "vitest";

import { expectNoIdentifiersInDefaultDom } from "./defaultDomPins";

/** Build a detached container so these cases need no React render. */
function mount(html: string): HTMLElement {
  const container = document.createElement("div");
  container.innerHTML = html;
  document.body.appendChild(container);
  return container;
}

describe("expectNoIdentifiersInDefaultDom — allowAriaLabelSelectors", () => {
  it("skips a snake_case aria-label on an element inside a matching selector, and on its descendants", () => {
    // closest() is what implements "and their descendants": the <button> does
    // not match `.import-yaml-actions` itself, its parent does.
    const container = mount(
      `<div class="import-yaml-actions">
         <button aria-label="Remove disabled component legacy_sink">Remove</button>
       </div>`,
    );
    expect(() =>
      expectNoIdentifiersInDefaultDom(container, {
        allowAriaLabelSelectors: [".import-yaml-actions"],
      }),
    ).not.toThrow();
  });

  it("STILL FAILS on the same aria-label outside the exempted subtree", () => {
    // The mutation kill for `if (ariaExempt.length > 0) continue;` and for
    // `?? ["*"]`: an exemption must be scoped, never global. Both mutations
    // make this case pass, which is why it is written.
    const container = mount(
      `<div class="import-yaml-actions"><button aria-label="Remove legacy_sink">Remove</button></div>
       <nav aria-label="Jump to legacy_sink"></nav>`,
    );
    expect(() =>
      expectNoIdentifiersInDefaultDom(container, {
        allowAriaLabelSelectors: [".import-yaml-actions"],
      }),
    ).toThrow();
  });

  it("STILL FAILS on snake_case VISIBLE TEXT inside the exempted subtree", () => {
    // The mutation kill for an exemption that leaks into the text scan. The
    // helper's contract is "the aria-label loop only"; this is the assertion
    // that makes that sentence executable.
    const container = mount(
      `<div class="import-yaml-actions">
         <button aria-label="Remove legacy_sink">legacy_sink</button>
       </div>`,
    );
    expect(() =>
      expectNoIdentifiersInDefaultDom(container, {
        allowAriaLabelSelectors: [".import-yaml-actions"],
      }),
    ).toThrow();
  });
});

describe("expectNoIdentifiersInDefaultDom — allowAriaLabelSelfSelectors", () => {
  it("skips a snake_case aria-label ON the matching element", () => {
    const container = mount(
      `<article class="pipeline-spec-card" aria-label="Node extract_invoice">
         <h4>Extract Invoice</h4>
       </article>`,
    );
    expect(() =>
      expectNoIdentifiersInDefaultDom(container, {
        allowAriaLabelSelfSelectors: ["article.pipeline-spec-card"],
      }),
    ).not.toThrow();
  });

  it("STILL FAILS on an aria-labelled DESCENDANT of a self-exempted element", () => {
    // THE mutation kill for the new matcher, and the whole reason it exists.
    // Implement it with `closest` instead of `matches` and this passes — which
    // is exactly the defect a first fix round shipped while claiming it had
    // narrowed the exemption to "two exact elements".
    const container = mount(
      `<article class="pipeline-spec-card" aria-label="Node extract_invoice">
         <button aria-label="Edit extract_invoice">Edit</button>
       </article>`,
    );
    expect(() =>
      expectNoIdentifiersInDefaultDom(container, {
        allowAriaLabelSelfSelectors: ["article.pipeline-spec-card"],
      }),
    ).toThrow();
  });
});
```

Run the red phase, and **be exact about which of the five actually goes red, because vitest transpiles with esbuild and does not typecheck** — the unknown `allowAriaLabelSelectors` and `allowAriaLabelSelfSelectors` properties are not runtime errors:

```bash
npx vitest run src/test/defaultDomPins.test.ts      # cases 1 and 4a FAIL; the rest pass
npx tsc --noEmit -p tsconfig.test.json              # all four FAIL: unknown option properties
```

- **Cases 1 and 4a are the red-phase evidence.** Pre-change neither exemption exists, so the snake_case aria-label throws and both `.not.toThrow()` assertions fail.
- **Cases 2, 3 and the descendant case pass pre-change by construction** — the unexempted labels throw either way. They are **mutation kills for the post-change implementation**, not red-phase evidence, and a reviewer must not read their green as proof the options work. That is precisely why they are written: the mutations they kill (`if (ariaExempt.length > 0) continue;`, `?? ["*"]`, exemption leaking into the text scan, and **implementing the self-only matcher with `closest` instead of `matches`**) all pass case 1.
- **The typecheck is what fails on all five**, so run it too and record it — that is the red phase for the two options' existence.

Step 2 turns cases 1 and 4a green and keeps the three mutation kills green for a different, load-bearing reason.

**Note on the `document.body.appendChild` in `mount`:** the helper reads `root.ownerDocument`, and a detached node's `ownerDocument` is still the jsdom document, so appending is not strictly required — but the existing consumer tests all pass a mounted RTL container, and matching that shape keeps this test measuring the same thing they do. Nothing needs cleaning up between cases; vitest's jsdom environment is per-file and these five containers do not interact (no `getByText`-style global queries are used).

- [ ] **Step 2: Extend the helper (turns Step 1 green)**

`defaultDomPins.ts` — replace the signature and the aria loop:

```ts
export function expectNoIdentifiersInDefaultDom(
  container: HTMLElement,
  options: {
    allowSelectors?: readonly string[];
    /** SUBTREE exemption (`closest`): the matched element AND its descendants
     *  are skipped by the aria-label loop. Use ONLY when the labels sit on
     *  CHILDREN of the selector — e.g. `.import-yaml-actions`, a container of
     *  two aria-labelled buttons. A container selector silently exempts every
     *  label added inside it later, so prefer the self-only option below.
     *  Exempts the aria-label loop only; visible text is still scanned
     *  (elspeth-13b69b5846). */
    allowAriaLabelSelectors?: readonly string[];
    /** SELF-ONLY exemption (`matches`): that element alone is skipped, and an
     *  aria-labelled DESCENDANT is still scanned. Use whenever the label is on
     *  the element the selector names — the Spec-tab `<article>` and the
     *  OptionRows region both are. This is the default choice; reach for
     *  `allowAriaLabelSelectors` only when children carry the labels. */
    allowAriaLabelSelfSelectors?: readonly string[];
  } = {},
): void {
  const clone = container.cloneNode(true) as HTMLElement;
  for (const selector of [...IDENTIFIER_SURFACES, ...(options.allowSelectors ?? [])]) {
    clone.querySelectorAll(selector).forEach((el) => el.remove());
  }
  const text = visibleText(clone);
  expect(text).not.toMatch(UUID_RE);
  expect(text).not.toMatch(HEX32_RE);
  expect(text).not.toMatch(SNAKE_RE);
  const ariaSubtreeExempt = options.allowAriaLabelSelectors ?? [];
  const ariaSelfExempt = options.allowAriaLabelSelfSelectors ?? [];
  for (const el of container.querySelectorAll("[aria-label]")) {
    const label = el.getAttribute("aria-label") ?? "";
    if (/^What does .* do\?$/.test(label)) continue; // ToolCallInfo trigger
    // SELF-only: `matches`, so a labelled DESCENDANT is still scanned.
    if (ariaSelfExempt.some((selector) => el.matches(selector))) continue;
    // SUBTREE: `closest`, for a container whose CHILDREN carry the labels.
    if (ariaSubtreeExempt.some((selector) => el.closest(selector) !== null)) continue;
    expect(label).not.toMatch(UUID_RE);
    expect(label).not.toMatch(HEX32_RE);
    expect(label).not.toMatch(SNAKE_RE);
  }
}
```

Update the header comment (`:1-10`) with **four** lines:

1. "The two aria options exempt accessible names that carry an author-chosen id by design."
2. "`allowAriaLabelSelfSelectors` matches with `matches()` — that element ONLY; a labelled descendant is still scanned. Prefer it."
3. "`allowAriaLabelSelectors` matches with `closest()` — the element AND its whole subtree. Use it only when the labels sit on CHILDREN of the selector; it silently exempts every label added inside that subtree later."
4. **The usage convention, which is the cheap half of the coverage problem the Roadmap's pin-ratchet row names:** "**Every component test that renders user-facing prose calls this helper.** Direct coverage of the helper itself lives in `defaultDomPins.test.ts`; extend it when you extend the options."

Line 4 is the one that matters to someone who has never seen this file — lines 1-3 only help a reader who already found it.

- [ ] **Step 3: The row, and a pin on both mounts**

`UnavailableComponentRow.tsx:45` — `<strong>{finding.component_id}</strong>` → `<strong><code>{finding.component_id}</code></strong>`; extend the header comment (`:1-8`): "The authored component id is the actionable name and stays — in `<code>`, the identifier register, because the user matches it against their own YAML (elspeth-59631ec7f7 ruling)."

**Two consequences of the `<code>` move, both worth stating rather than discovering:**

- **The pin added in this same step proves nothing about that row's id.** `defaultDomPins.ts:18-22` strips `code` elements from the clone before scanning, so after this change the id is outside the text scan by construction. The pin is still worth having — it covers the reason label, the plugin display name and the two aria-labels — but the commit message must not claim "the unavailable row's id is pinned clean". It is *classified*, not *pinned*.
- **Check the rendered size.** `<code>` inside `<strong>` is valid phrasing content and announces acceptably (no mainstream screen reader announces `<code>` boundaries at default verbosity; `<strong>` maps to `role="strong"`, likewise usually silent). The visual risk is the usual monospace shrink: confirm the catalog stylesheet gives `code` an explicit `font-size` that does not fall below the surrounding text. A sub-14px identifier is hard to transcribe, which is the entire reason for showing it raw. Not a WCAG 1.4.4 failure; a legibility check, one line in the review.

  2.5.3 Label in Name is satisfied for the sibling buttons either way: the accessible name "Remove disabled component legacy_sink (Legacy Sink)" contains the visible label "Remove".

In `CatalogDrawer.test.tsx`, add the pin as a new `it()` inside `describe("CatalogDrawer — unavailable-components notice placement")` (`:735`). That describe's own `beforeEach` (`:743-762`) already seeds `useSessionStore.setState({ compositionState: { plugin_policy_findings: [FINDING] } } as never)` with `FINDING.component_id = "legacy_sink"`, so the new test needs no setup of its own — there is NO named render helper in this file (each `it()` calls `render(...)` inline; verified 2026-08-30, `renderWithUnavailable` and every synonym return zero hits):

```tsx
  it("default DOM of the unavailable-components section passes the shared pin with the button names exempted", async () => {
    const { container } = render(<CatalogDrawer isOpen onClose={() => {}} />);
    // Present FIRST. The notice is gated on catalog load AND on the finding's
    // snapshot_fingerprint matching, so a synchronous pin would scan an empty
    // DOM and pass vacuously — the same trap the sibling tests in this
    // describe guard against.
    await screen.findByRole("region", { name: "Unavailable saved components" });
    expectNoIdentifiersInDefaultDom(container, {
      allowAriaLabelSelectors: [".import-yaml-actions"],
    });
  });
```

(Import `expectNoIdentifiersInDefaultDom` from `@/test/defaultDomPins`. The exemption selector is `.import-yaml-actions` (`UnavailableComponentRow.tsx:49`) — the `<div>` `UnavailableComponentRow` wraps around `actions`, and therefore the tightest `closest()` ancestor of the two aria-labelled Remove/Replace buttons — NOT the whole `.validation-banner-error-item` row (`:43`), which would also exempt any future aria-label added to the row's own content. Per the exemption-scope ruling, a container selector is used here *because the labels being exempted are on children*; that is the justification, and it does not generalise.)

**Expected to pass, with the reasoning recorded so a failure is diagnosable rather than mysterious:** the mocked catalog in this file (`CatalogDrawer.test.tsx:13-87`) ships single-word plugin names (`csv`, `uppercase`, `json`) with prose descriptions and `principal_scope: "local:alice"` (a colon, not an underscore); `unavailableReasonLabel` (`CatalogDrawer.tsx:148-156`) maps `plugin_not_installed` → `"Not installed"`; and after this step's change `component_id` sits inside `<code>`, which `IDENTIFIER_SURFACES` strips. If it fails, the new token is the thing to look at — not the exemption.

In `src/components/sidebar/ImportYamlModal.test.tsx` the existing pin sits on a YAML with no unavailable component (ticket note); add one test that feeds a preflight response carrying one such finding and calls the helper with the same exemption. **The field is `plugin_policy_findings`, not `disabled_components`** (which has zero hits in that file; the wire name is confirmed at `ImportYamlModal.tsx:880`, `result.plugin_policy_findings ?? []`). The directly reusable pattern is the existing `it("renders sanitized disabled-component repair actions without fetching private schema", …)` at `:1048`, whose `api.importCompositionYaml` mock resolves with a `plugin_policy_findings` array at `:1055` and which already drives the `UnavailableComponentRow` render path via `getByRole("region", { name: /unavailable saved components/i })`. Copy its mock and its await, then pin.

**Pass BOTH options, not just the aria one.** The file's existing pin at `ImportYamlModal.test.tsx:486` is `expectNoIdentifiersInDefaultDom(container, { allowSelectors: ["textarea"] })`, because the YAML the test types into the textarea is raw pipeline YAML and is wall-to-wall snake_case. Whether the textarea is still mounted once the preflight response has rendered the unavailable-components region decides whether it is needed here. **Do not decide that at implementation time by trying it and adding the option if red** — check the component: if `ImportYamlModal` unmounts the textarea in the phase that shows `plugin_policy_findings`, pass only the aria exemption and say so in a comment; if it does not, pass both:

```tsx
expectNoIdentifiersInDefaultDom(container, {
  allowSelectors: ["textarea"],                       // raw YAML is an identifier surface, as at :486
  allowAriaLabelSelectors: [".import-yaml-actions"],  // Remove/Replace name the component by id
});
```

Run: `npx vitest run src/test/defaultDomPins.test.ts src/components/catalog src/components/sidebar/ImportYamlModal.test.tsx` → PASS. (Note the explicit `defaultDomPins.test.ts` path: `src/test` as a directory argument would have collected it, but naming it makes Step 1's green run visible in the output rather than buried among the integration tests in that directory.)

- [ ] **Step 4: preferencesStore describe move (Wave 1 deferral, test-only)**

`preferencesStore.test.ts`: the test `it("resetTutorial clears tutorial_completed_at through the PATCH contract", …)` (`:397-434`) exercises the four resume fields but sits in the top-level `describe("preferencesStore")`; the resume-state describe is `describe("preferencesStore — tutorial resume state (elspeth-918f4434b3)")` at `:868`, which has its own `beforeEach` (`resetStore` + `vi.clearAllMocks()`). Cut the test verbatim — **`:397-432`, not `:397-434`**: the `it(...)` closes at `:432`, `:433` is blank, and `:434` opens `it("dismissDefaultChangedBanner persists timestamp", …)`, which cutting through `:434` would decapitate. Paste it as the last `it` of that describe. No edits to its body.

Run: `npx vitest run src/stores/preferencesStore.test.ts` → PASS. **The acceptance criterion is NOT "the same test count".** The test lands in a describe with its own `beforeEach` (`resetStore` + `vi.clearAllMocks()`), so it could now pass for a different reason — e.g. an assertion that was load-bearing against leaked state and is now trivially satisfied — and both the count and the pass/fail verdict are invariant under that. The honest criterion is the one this step already imposes: **`git diff` shows the test's body byte-unchanged and only its position moved**, and it still exercises all four resume fields. Check the diff, not the count.

- [ ] **Step 5: Commit (two commits — the helper/row are a ticket, the move is hygiene)**

```bash
git add src/elspeth/web/frontend/src/test/defaultDomPins.ts src/elspeth/web/frontend/src/test/defaultDomPins.test.ts src/elspeth/web/frontend/src/components/catalog/UnavailableComponentRow.tsx src/elspeth/web/frontend/src/components/catalog/CatalogDrawer.test.tsx src/elspeth/web/frontend/src/components/sidebar/ImportYamlModal.test.tsx
git commit -m "test(pins): allowAriaLabelSelectors on the default-DOM pin; unavailable-component id in <code>; pin both mounts (elspeth-13b69b5846, elspeth-59631ec7f7 catalog half)"
git add src/elspeth/web/frontend/src/stores/preferencesStore.test.ts
git commit -m "test(preferences): move the resetTutorial resume-fields test into the tutorial-resume describe (Wave 1 deferral)"
```

Close `elspeth-13b69b5846` at closeout (Task 11).

---

### Task 3: Lift the connection-topology model into `lib/graphTopology.ts` (mechanism for Task 4)

**Files:**
- Create: `src/elspeth/web/frontend/src/lib/graphTopology.ts`
- Create: `src/elspeth/web/frontend/src/lib/graphTopology.test.ts`
- Modify: `src/elspeth/web/frontend/src/components/inspector/GraphView.tsx` — FOUR edits, no more: (1) `:86-182`'s four helpers move out and come back as imports; (2) the producer registry is hoisted into an exported module-level `buildProducerRegistry(state)` — **a NON-CONTIGUOUS extraction: `:1245-1260` plus `:1298-1351`, deliberately SKIPPING `:1262-1296`** (see the hoist ruling for the exact split and why); (3) the `useMemo` REBINDS the result (`const connectionProducers = buildProducerRegistry(compositionState);`) because the map escapes to `:1377`, `:1469` and `:1527`; (4) one import line. `:1313`, `:1365-1367` call sites unchanged. **Four things, not three** — a pre-fix draft said three and cited `:1298-1350`, a range that compiles for nobody.
- Modify: `src/elspeth/web/frontend/src/api/guidedDecoder.ts` (`:75-76` — `COALESCE_POLICIES` / `COALESCE_MERGES` move out and come back as imports; every other line untouched)
- Modify: `src/elspeth/web/frontend/src/components/chat/guided/SchemaFormTurn.tsx` (`:74` — the bare `"discard"` becomes `DISCARD_CONNECTION`; one line, and it is in this task's `git add`)
- Modify + rename: `tests/unit/web/composer/test_graph_view_self_publishing_parity.py` → `tests/unit/web/composer/test_graph_topology_parity.py` — **re-anchor the path constant and extend the parity coverage; this file is the reason Task 3 is a backend-touching task** (Step 2b). Repo-root paths, not frontend ones.
- **Unchanged, must stay green:** `src/elspeth/web/frontend/src/components/inspector/GraphView.test.tsx`, `src/elspeth/web/frontend/src/api/guidedDecoder.test.ts`

**Interfaces:**
- Produces, all from `lib/graphTopology.ts`. Four are lifted VERBATIM from `GraphView.tsx`:
  - `IMPLICIT_SELF_PUBLISHING_NODE_TYPES: ReadonlySet<string>` — `queue`, `coalesce`, `aggregation` (`GraphView.tsx:136-140`)
  - `publishedSuccessConnection(node): string | null` (`:142-153`)
  - `FAN_IN_NODE_TYPES: ReadonlySet<string>` — `row_union`, `coalesce` (`:155-158`)
  - `branchEntries(branches): [string, string][]` (`:175-182`)
  A fifth is lifted from `GraphView.tsx:1245-1260` + `:1298-1351` with its ReactFlow decoration stripped (see the producer-registry ruling for the non-contiguous split):
  - `buildConnectionProducers(state): Map<string, string[]>` — connection name → producing component ids
  Two more are lifted from `api/guidedDecoder.ts:75-76`, retyped as `as const` tuples so they carry a union type (see the member-set ruling):
  - `COALESCE_POLICIES` + `type CoalescePolicy`
  - `COALESCE_MERGES` + `type CoalesceMerge`
  And one is new, naming the ROUTING-VALUE sentinel (see the `DISCARD_CONNECTION` ruling for the exact two pre-existing sites and the vocabulary it must not be confused with):
  - `DISCARD_CONNECTION = "discard"`
- Consumes: **types, plus `sortedSourceEntries` from `utils/compositionState.ts`** — which is itself type-only (`import type { CompositionState, SourceSpec } from "@/types/index"` is its whole import block), so no React, no store and no CSS enter the dependency graph. **The constraint binds `graphTopology.ts`, NOT `graphTopology.test.ts`:** the cross-check test in the producer-registry ruling imports `buildProducerRegistry` from `@/components/inspector/GraphView`, which pulls in React and ReactFlow. That is a test file — it creates no production edge and no cycle — and it is stated here so the two things this wave adds do not read as contradicting each other. That is the load-bearing constraint, **not** the directory: `src/lib/` is not a strict bottom layer in this tree (`lib/validationHumaniser.ts:38` imports `@/components/catalog/pluginDisplayName`, and `lib/composer-events.ts:1` imports from `@/components/workspace/workspaceTypes`), so "it is in `lib/`" would not by itself guarantee the absence of a cycle. What guarantees it is that everything this module imports is type-only or type-only-transitively — keep that stated as the rule. Same contract `lib/validationHumaniser.ts:13-16` and `components/chat/guided/stepLabels.ts:5-7` state for themselves. Typed structurally against the fields it reads so both `NodeSpec` and the graph's local node shape satisfy it.
- **Blast radius, RE-measured from the REPOSITORY ROOT (a pre-fix draft measured this from `src/elspeth/web/frontend`, where the pathspec `-- src tests` resolves to the frontend's own `src/` and `tests/e2e` and cannot see `tests/unit/` — the pathspec was frontend-scoped and the conclusion was stated tree-wide).** Run from `/home/john/elspeth`:

  ```bash
  git grep -n "publishedSuccessConnection\|branchEntries\|FAN_IN_NODE_TYPES\|IMPLICIT_SELF_PUBLISHING_NODE_TYPES" -- src tests
  ```

  **Inside the frontend:** hits in `GraphView.tsx` only — **four** declarations (`:136` `IMPLICIT_SELF_PUBLISHING_NODE_TYPES`, `:142` `publishedSuccessConnection`, `:155` `FAN_IN_NODE_TYPES`, `:175` `branchEntries`), three call sites (`:1313`, `:1365`, `:1367`), three comment mentions (`:97`, `:1670`, `:1857`), and one intra-module reference at `:150` — eleven hits, not nine. **No frontend test file matches**, which is why `GraphView.test.tsx` and `guidedDecoder.test.ts` can be required to stay untouched.

  **Outside the frontend — this is what the frontend-scoped grep missed, and it is a Python↔TypeScript parity gate that Task 3 BREAKS as written.** `tests/unit/web/composer/test_graph_view_self_publishing_parity.py` reads `GraphView.tsx` **off disk** by hard-coded path (`:41-42`, `_GRAPH_VIEW_PATH = _PACKAGE_ROOT / "web" / "frontend" / "src" / "components" / "inspector" / "GraphView.tsx"`) and pins it to `_producer_resolver._IMPLICIT_SELF_PUBLISHING_NODE_TYPES`. All THREE of its tests fail once the symbols move:

  | Test | Anchor | Why it fails |
  |---|---|---|
  | `test_graph_view_self_publishing_set_matches_the_python_authority` | `:48-50` regex `const\s+IMPLICIT_SELF_PUBLISHING_NODE_TYPES\s*:\s*ReadonlySet<string>\s*=\s*new\s+Set\(…`, asserted `len(matches) == 1` at `:58` | the declaration is no longer in `GraphView.tsx` → 0 matches → `AssertionError` |
  | `test_the_ts_helper_consults_the_set_rather_than_on_success_alone` | `:97` `"function publishedSuccessConnection(" in text`; `:102` `"IMPLICIT_SELF_PUBLISHING_NODE_TYPES.has(node.node_type)" in text` | both strings leave `GraphView.tsx` with the lift, and Step 2 explicitly does NOT import the set back in |
  | `test_graph_view_file_is_readable` | `:113` vacuity smoke over the same parse | same cause as the first |

  The regex itself SURVIVES the move — it is not line-anchored, so `const …` still matches inside `export const …`. **Only the path is wrong.** The test anticipated exactly this and says so in its own failure message at `:59-62`: *"The declaration moved, was renamed, or Prettier rewrote its shape — **re-anchor this regex rather than deleting the parity test**, which is the only thing pinning the TS copy to the Python authority."* Task 3 Step 2b does the re-anchor; Step 3 runs the pytest; Task 11 Step 3 lists the module.

  **This is also the strongest evidence FOR Task 3.** That test's docstring records the same two incidents this ruling cites (the `aggregation` miss; session 3f02c8fa) and states the thesis outright: *"The TS copy cannot import it, so nothing but this test stops the two from drifting."*

  `COALESCE_POLICIES` / `COALESCE_MERGES` genuinely ARE frontend-only — re-verified from the repository root, hits in `api/guidedDecoder.ts:75-76` and its two membership checks at `:467`/`:468`, nothing under `tests/`.

  So this task modifies **three** production/test files (`GraphView.tsx`, `api/guidedDecoder.ts`, and `test_graph_view_self_publishing_parity.py`, which Step 2b also renames to `test_graph_topology_parity.py`) and adds two (`lib/graphTopology.ts`, `lib/graphTopology.test.ts`). The claim "no test file changes at all" is deleted: it was false, and it was the sentence that made Task 3's own acceptance procedure structurally blind to the breakage.

- **Ruling — which mirrors are GATED and which are convention-only. State it; do not imply uniform protection.** The module header says every rule mirrors a named backend authority *by path*, which is a discovery aid, not a gate. Before this task, one of the four mirrors had an executable gate and three did not:

  | Backend authority | Frontend mirror | Gate before Task 3 | Gate after Task 3 |
  |---|---|---|---|
  | `_producer_resolver.py:98` + `_IMPLICIT_SELF_PUBLISHING_NODE_TYPES` | `publishedSuccessConnection`, `IMPLICIT_SELF_PUBLISHING_NODE_TYPES` | `test_graph_view_self_publishing_parity.py` | same test, re-anchored + renamed `test_graph_topology_parity.py` |
  | `guided/connection_consumers.py:32` — the literal `("coalesce", "row_union")` fan-in arm | `FAN_IN_NODE_TYPES` | **none** | **added** (Step 2b) |
  | `core/config.py:1007`, `:1011` policy/merge `Literal`s | `COALESCE_POLICIES`, `COALESCE_MERGES` | **none** | **added** (Step 2b) |
  | `core/config.py:984-986` `CoalesceSettings.branches` = "Branch identity → input connection mapping" | `branchEntries` | none | **none — convention only.** It mirrors a *docstring's semantics*, not an enumerable value; there is nothing to regex. Recorded here so the gap is stated rather than assumed away. |

  I enumerated every Python test that reads a frontend source file — there are five (`test_graph_view_self_publishing_parity.py`, `tests/unit/web/catalog/test_audit_characteristic_vocabulary_parity.py`, `test_tool_call_description_parity.py`, `test_gate_projection_fixture.py`, and the `ComposerProgressReason` mirror at `test_progress.py:603`) — and none covered the bottom three rows. Extending the re-anchored test is marginal cost because Step 2b already opens that file, and it closes the exact gap the next ruling was overclaiming.

**Ruling — extract and reuse, do not re-derive.** The Spec tab (Task 4) needs to answer "which component is on the other end of this connection?". That question already has an answer in this codebase, in `GraphView.tsx`, mirrored from two named backend authorities, and it is *scarred*: `publishedSuccessConnection`'s comment records session 3f02c8fa (a working fork/coalesce pipeline drawn as two disconnected fragments because the rule was re-derived from `on_success` alone) and `branchEntries`' comment records `elspeth-625e85c59b` (a coalesce drawn with a single arm because `branches` was assumed to be a map). A second module re-deriving the same rules is a second place for those two incidents to recur, and the pre-fix draft of this plan did exactly that and inverted the direction of `branches` in the process. The authorities:

- `src/elspeth/core/config.py:984-986` — `CoalesceSettings.branches` is a **"Branch identity → input connection mapping"**. A fan-in node's `branches` are its INPUTS. Not its outputs.
- `src/elspeth/web/composer/guided/connection_consumers.py:31-40` — the canonical consumer projection. For `coalesce`/`row_union` it registers each BRANCH connection as consumed by the node and then `continue`s; it **never** registers `node.input` for a fan-in node, because that scalar is only the backend-compatible first-branch placeholder.
- `_producer_resolver.published_success_connection` — `on_success` if set; else the node id for `queue`/`coalesce`/`aggregation`; else nothing. `GraphView.tsx:112-135` already mirrors it and says so.

Cost if wrong: the Spec tab and the Graph tab disagree about the same pipeline, on the exact shape (a coalesce) where they have already disagreed once in production.

**Ruling — the PRODUCER REGISTRY is lifted too, because otherwise Task 4 re-derives it one level up.** A pre-fix draft lifted only the four leaf helpers and let Task 4's `buildConnectionIndex` assemble its own producers map over them. That is the same defect this task exists to fix, moved up a level: the scarred *primitives* would be shared and the *rule they feed* — which fields publish a connection, and what happens when several publish the same one — would stay private in `GraphView.tsx`. The existing authority is `GraphView.tsx:1245-1260` + `:1298-1351` (see the non-contiguous-hoist ruling below for why those two blocks and not the span between them):

- `:1250-1254` `type ProducerInfo = { nodeId; edgeType; label }` and `:1255` `const connectionProducers = new Map<string, ProducerInfo[]>()`
- `:1256-1260` `registerProducer`
- fed by exactly four rules: sources' `on_success` (`:1299-1307`), a node's `publishedSuccessConnection` (`:1313-1320`), `on_error` (`:1321-1327`), `routes` (`:1328-1336`), and a gate's `fork_to` when a route is `"fork"` (`:1337-1350`)
- with the multimap discipline commented as load-bearing at `:1246-1249`: *"ELSPETH allows MANY producers to publish one connection name ONLY when a declared queue node consumes it (structural fan-in, ADR-028). So this is a MULTIMAP, not one-producer-per-connection: overwriting would silently drop every producer but the last and misrender the intentional fan-in."*

**A verbatim lift is not available** — `ProducerInfo` carries `edgeType` and `label`, which are ReactFlow edge-drawing concerns the Spec tab has no use for. So the lift is *shape-reducing, rule-preserving*:

```ts
/**
 * Connection name -> ids of the components that PUBLISH it.
 *
 * Lifted from GraphView.tsx:1245-1260 (ProducerInfo, the map, registerProducer)
 * and :1298-1351 (the five registration blocks), which together held the only
 * statement of the rule "which fields publish a connection". A MULTIMAP, not one producer per
 * connection: ELSPETH allows many producers on one connection name under a
 * declared queue (structural fan-in, ADR-028), and overwriting would silently
 * drop every producer but the last and misrender the intentional fan-in.
 *
 * This is the registration rule ONLY. GraphView decorates each entry with its
 * own `edgeType`/`label` for ReactFlow, and applies three further DRAWING
 * rules on top that are NOT topology and deliberately stay there:
 * queue-as-sole-canonical-producer (GraphView.tsx:1262-1269), the row_union
 * authoritative-outbound semantics (:1270-1296), and the phase-1 alias dedup
 * (:1353-1417).
 */
export function buildConnectionProducers(state: CompositionState): Map<string, string[]> {
  const producers = new Map<string, string[]>();
  const push = (connection: string, producerId: string): void => {
    const existing = producers.get(connection);
    if (existing === undefined) producers.set(connection, [producerId]);
    else if (!existing.includes(producerId)) existing.push(producerId);
  };
  // sortedSourceEntries, not Object.entries: this is what GraphView.tsx:1299
  // uses, and a faithful lift keeps the deterministic ordering. Importing it
  // does not break the leaf contract — utils/compositionState.ts imports only
  // types (`import type { CompositionState, SourceSpec } from "@/types/index"`),
  // so no React and no store enters this module's dependency graph.
  for (const [sourceName, source] of sortedSourceEntries(state)) {
    if (source.on_success && source.on_success !== DISCARD_CONNECTION) push(source.on_success, sourceName);
  }
  for (const node of state.nodes) {
    const published = publishedSuccessConnection(node);
    if (published && published !== DISCARD_CONNECTION) push(published, node.id);
    if (node.on_error && node.on_error !== DISCARD_CONNECTION) push(node.on_error, node.id);
    if (node.routes) {
      for (const target of Object.values(node.routes)) {
        if (target !== "fork" && target !== DISCARD_CONNECTION) push(target, node.id);
      }
    }
    if (node.node_type === "gate" && node.routes
        && Object.values(node.routes).includes("fork") && node.fork_to) {
      for (const branchConnection of node.fork_to) push(branchConnection, node.id);
    }
  }
  return producers;
}
```

Two deliberate divergences from `GraphView.tsx`, both stated so neither is discovered later as a bug:

1. **The two SENTINELS — `"fork"` and `DISCARD_CONNECTION` — are skipped here; GraphView registers both.** GraphView registers the literal `"fork"` as a connection name and then never looks it up (its `fork_to` loop supplies the real connections), and it does not special-case `"discard"` at all. Neither is a connection: `_producer_resolver.py:208` decides `discard`, and `"fork"` is a route marker. Registering either as a key would put a sentinel in the Spec tab's index as a resolvable connection, so that some other component's `input: discard` could resolve to a producer. Skipping them is not a behaviour change for GraphView — GraphView keeps its own `registerProducer` call and its own decoration; `buildConnectionProducers` is a second, reduced view for consumers who want topology without edges. **This is the one place Task 3 is not a byte-identical lift, and it is why `GraphView.tsx` keeps its `registerProducer` block rather than calling the new function.** It is also why `specRouting.ts` has no private `nodeTargets` helper: a pre-fix draft carried one, which was this module's own restatement of the same four rules with the same two sentinel filters — the duplication one level up that this lift removes.
2. **The four registration rules are exactly GraphView's four.** In particular a source's `on_validation_failure` and an output's `on_write_failure` are NOT registered here, because GraphView does not register them (verified: `on_validation_failure` appears in `GraphView.tsx` only at `:628`, a config-panel field, and `on_write_failure` not at all). Task 4 needs both — see Task 4's index ruling, which adds them **on top of** this function rather than inside it, so the Graph tab's behaviour cannot change.

**This is a PARTIAL lift, and the condition attached to declining the full one is not optional.** The full remedy would have `GraphView.tsx` consume `buildConnectionProducers` and decorate the result with its own `edgeType`/`label`. That is not available: `ProducerInfo` carries those two fields *per registration* (a node's `on_error` registration is labelled `"error"`, each `routes` entry is labelled with its own alias), and a `Map<string, string[]>` cannot reconstruct them — GraphView would have to re-walk the composition to re-derive the labels, which is the duplication again with an extra step. So GraphView keeps its `registerProducer` block, and the tree ends up with **two implementations of "which fields publish a connection"** in a wave whose thesis is one-rule-one-place. Stating that plainly is half of what is owed; **the other half is a cross-check pin, and without it this task should not be dispatched.** Add it to `graphTopology.test.ts` — one shared fixture, both indexes, one assertion:

```ts
import { buildConnectionProducers } from "./graphTopology";
import { buildProducerRegistry } from "@/components/inspector/GraphView";

describe("buildConnectionProducers agrees with GraphView's producer registry", () => {
  it("registers the same producers for the same coalesce/fork composition", () => {
    // The two implementations that this task deliberately did NOT merge. They
    // diverge only in the two sentinels and in ReactFlow decoration, so the
    // KEY SET and the producer-id set per key must match exactly. Drift on
    // this axis is what misrendered a working fork/coalesce pipeline as two
    // disconnected fragments (session 3f02c8fa) and drew a coalesce with a
    // single arm (elspeth-625e85c59b) — the two incidents this module exists
    // to stop recurring.
    const state = SHARED_FANIN_FIXTURE;   // one fixture, used by both assertions
    const lifted = buildConnectionProducers(state);
    const graphView = buildProducerRegistry(state);   // ProducerInfo[] per key

    const graphViewKeys = new Set([...graphView.keys()].filter(
      (key) => key !== "fork" && key !== DISCARD_CONNECTION,
    ));
    expect(new Set(lifted.keys())).toEqual(graphViewKeys);
    for (const key of graphViewKeys) {
      expect(new Set(lifted.get(key))).toEqual(
        new Set((graphView.get(key) ?? []).map((producer) => producer.nodeId)),
      );
    }
  });
});
```

`SHARED_FANIN_FIXTURE` must exercise all four rules and both sentinels at once: a source with `on_success`, a node whose `on_success` is implicit (a queue or non-terminal coalesce), a node with `on_error`, a gate with a `routes` map containing both a real target and `"fork"` plus a `fork_to` array, and one `on_error: "discard"`. Declare it once in the test file and use the same object in both assertions — the point is a SHARED fixture, not two that happen to agree.

**On reaching GraphView's registry from a test.** It is built inside a `useMemo` in the component, so it is not importable today. Two honest options, and **the plan picks the second**: (a) export a `__testing` handle from `GraphView.tsx`, which adds a production export purely for a test; (b) **extract the registry-building code into a named module-level function inside `GraphView.tsx`** — `function buildProducerRegistry(state): Map<string, ProducerInfo[]>` — export it, and have the `useMemo` call it. (b) is a behaviour-preserving extraction of code this task is already reading, it makes the `git diff` show a moved block rather than a test-only hatch, and it is the shape the rest of this task uses.

**Ruling — the hoist is NON-CONTIGUOUS, and the exact split is `:1245-1260` + `:1298-1351`, SKIPPING `:1262-1296`. Do not cut `:1298-1350`; it compiles for nobody.** A pre-fix draft cited that range at three sites and `:1245-1350` at three more, and both are wrong in opposite directions. The layout, verified line by line:

| Lines | Content | Hoist? |
|---|---|---|
| `:1245-1249` | the multimap doctrine comment — `:1245` is its heading ("Producer registry: connection_point_name → producers."), `:1246-1249` the ADR-028 body | **YES** — it is the reason the shape is a multimap, and cutting from `:1246` orphans the heading |
| `:1250-1254` | `type ProducerInfo = { nodeId; edgeType: EdgeFlowType; label }` | **YES** — it is the return type |
| `:1255` | `const connectionProducers = new Map<string, ProducerInfo[]>()` | **YES** |
| `:1256-1260` | `function registerProducer(connection, producer)` | **YES** — the loops' only writer |
| `:1262-1274` | `queueIds`, `rowUnionIds` | **NO** |
| `:1275-1296` | `authoritativeRowUnionOutboundSemantics` + `registerAuthoritativeRowUnionOutbound` | **NO** |
| `:1298-1351` | the source loop and the node loop (`:1351` closes the node loop; `:1352` is blank) | **YES** |

Three consequences, each of which a lane hits within minutes of starting:

1. **`:1298-1350` is too NARROW — twice over.** It excludes `ProducerInfo`, `connectionProducers` and `registerProducer` (`:1250-1260`), so a module-level function has no return type, nothing to register into, and no `registerProducer` in scope. It also stops one line short: the node loop closes at `:1351`, so a literal cut leaves an orphan `}` — the same defect as the `:86-181` cut this task already corrects one section earlier.
2. **`:1245-1350` is too WIDE.** It sweeps in `:1262-1296`, which is drawing-pass state, not registry state: `queueIds` (`:1265`), `rowUnionIds` (`:1271`), `authoritativeRowUnionOutboundSemantics` (`:1275`) and `registerAuthoritativeRowUnionOutbound` (`:1279`) are consumed far downstream — `queueIds` at `:1371`, `:1468`, `:1510`; `rowUnionIds` at `:1475`, `:1533`, `:1539`, `:1584`, `:1614`, `:1646`, `:1695`; `authoritativeRowUnionOutboundSemantics` at `:1284`, `:1295`, `:1690` — all inside the same `useMemo`, and **none of them is used by `:1298-1351`**, so the split is clean. (Do not confuse those with `:1377`, `:1469` and `:1527`, which are `connectionProducers` reads — they are why the hoisted function must RETURN the map, a different consequence covered in point 3.) Hoisting them would also move `authoritativeRowUnionOutboundSemantics` at `:1275`, which is the symbol Exception (b)'s replacement comment tells the reader to look above — silently breaking that pointer in the very commit that repairs it.
3. **`connectionProducers` ESCAPES the hoisted region.** It is read at `:1377`, `:1469` and `:1527`, so `buildProducerRegistry` must `return connectionProducers;` and the `useMemo` must **rebind** it: `const connectionProducers = buildProducerRegistry(compositionState);` in place of the old declaration. The `useMemo` diff is a rebinding, not merely "now calls it".

`EdgeFlowType` needs no attention — it is already module-level at `GraphView.tsx:208`, so `ProducerInfo` hoists without dragging it.

**The block starts at `:1245`, not `:1246` — re-measured, and both the round-2 sign-offs and the brief that relayed them said `:1246`.** `:1245` is `// Producer registry: connection_point_name → producers.`, the HEADING line of the multimap doctrine comment whose body runs `:1246-1249`; `:1244` is the blank separator and `:1243` is the tail of an unrelated numbered comment. Cutting from `:1246` would leave that heading orphaned in the `useMemo`, dangling above `queueIds` and describing a registry that is no longer there — the same defect class as the `:86-181` truncation this task already corrects, arriving in the correction for it. **`:1245-1260` + `:1298-1351`.**

**So Step 3's purity check expects FOUR things, not three** — see it for the enumerated list. Risk if this is left as it was: a lane either stalls at the compile error, or hoists too much and silently changes when the row-union semantics map is populated relative to the producer registry, inside the 2004-line `useMemo` that draws every edge. `GraphView.test.tsx:2018`/`:2121` are the cases that would catch that, which is why this task requires them green and untouched — but the plan must not send a lane into that edit with a wrong range and a purity rule that forbids the correct diff.

Cost if wrong: the two implementations drift on a rule the cross-check does not cover — specifically the *ordering* within a producer list (the test compares sets, because GraphView sorts by `nodeId` at `:1378` for edge-drawing and the Spec tab has no ordering requirement) or the ReactFlow labels, which the Spec tab does not read. The key set and the membership — the axis both production incidents were on — cannot drift silently.

**Ruling — `DISCARD_CONNECTION` names the ROUTING-VALUE sentinel and exactly TWO pre-existing sites; it is NOT the `ProposalEndpointKind` union literal.** A pre-fix draft said "three sites spelled it as a bare literal". That is not a miscount — it is a claim about the wrong set of sites, and it was being used to justify a new exported symbol. The tree holds ~40 production occurrences of `"discard"` across nine modules, and they are **two different vocabularies**:

- **The routing-value / connection-name sentinel** — the concept `_producer_resolver.py:208` decides (`if connection_name is None or connection_name == "discard"`). Exactly **two** production sites spell it by hand: `components/workspace/PipelineSpecView.tsx:52` (`if (value === "discard") return "dropped (recorded in the audit trail)"`) and `components/chat/guided/SchemaFormTurn.tsx:74` (`next["on_validation_failure"] = "discard"`). Plus prose at `tutorial/copy.ts:158`, which is copy and stays copy. **This is what `DISCARD_CONNECTION` names.**
- **The `ProposalEndpointKind` discriminated-union literal — a DIFFERENT vocabulary that happens to share the word, and it STAYS a literal type.** Declared at `types/guided.ts:600` (`ProposalTargetEndpoint = ProposalEndpoint | { kind: "discard" }`), `:802`, and `api/guidedDecoder.ts:250` (`type ProposalEndpointKind = "source" | "node" | "output" | "discard"`); consumed at `guidedDecoder.ts:266,268,269,274,480,483,644,646,975,1005,1595,1598,1793,1796`, `ProposePipelineTurn.tsx:197,218,223,227,228,242,243,281,282`, `WireStageTurn.tsx:39,84,308,323,324`, `behaviorSummary.ts:34,35`, `ReadOnlyPipelineGraph.tsx:17`. **A `const DISCARD_CONNECTION` cannot replace any of these and must not be pointed at them:** TypeScript narrowing on a discriminated union requires the literal in the type position, and a `const` typed `string` breaks it. ~38 sites.

**Scope, stated as an instruction rather than left to inference: convert `PipelineSpecView.tsx:52` (Task 4 does this, moving the check into `RoutingDd`) and `SchemaFormTurn.tsx:74` (one line, in Task 3's commit). Convert nothing else. If you find yourself editing `guidedDecoder.ts` or `ProposePipelineTurn.tsx` for this, stop — you are in the other vocabulary.** The module header says so too, so the constant carries its own scope. Cost if wrong: someone collapses two vocabularies into one constant whose docstring cites `_producer_resolver.py:208`, and the guided proposal decoder's union narrowing breaks — a compile error, loudly, which is the one saving grace.

**Ruling — the LOCATION and the NAME are the fix, not packaging around it: `src/lib/graphTopology.ts`.** The failure mode is discoverability, so a module nobody finds fixes nothing. Two placements are wrong for that reason and are ruled out explicitly: keeping it under `components/inspector/` (nobody working on the Spec tab greps `inspector/`) and putting it under `components/workspace/` beside `specRouting.ts` (the same mistake pointed the other way — the Graph tab would then import out of the Spec tab's directory). `src/lib/` is the repo's own home for exactly this: `lib/validationHumaniser.ts:13-16` documents the leaf-no-React contract, and `components/chat/guided/stepLabels.ts:5-8` states the archetype in the plainest possible terms — *"Deliberately a LEAF module … It replaces three hand-mirrored STEP_LABELS copies that had drifted."* **The repo has already had this exact defect on the label axis and already fixed it with exactly this remedy.** That is why this plan reuses labels correctly and got topology wrong: topology is the one axis that never got the treatment. Nothing new is being invented here.

Three naming constraints follow, and they are requirements, not preferences:

1. **Keep the symbol names a searcher would actually type.** `publishedSuccessConnection`, `branchEntries`, `FAN_IN_NODE_TYPES`, `IMPLICIT_SELF_PUBLISHING_NODE_TYPES` already are those names. Do not "improve" them during the move.
2. **The module header must name the backend authority file by path**, so that `git grep _producer_resolver` run from the Python side finds the frontend mirror. The authority is `src/elspeth/web/composer/_producer_resolver.py`, function `published_success_connection` at `:98` — note the path: it is under `web/composer/`, NOT `core/`, and the same goes for `web/composer/guided/connection_consumers.py`. Both were miscited as `core/` during review; a wrong path in a header comment is worse than none, because it teaches the next reader that the mirror has no authority.
3. **The header speaks in the voice `pluginDisplayName.ts:76` uses** — "THE frontend's single title-casing implementation (elspeth-d2de348437)" — i.e. it claims the singular, so a future author who is about to write a second one reads the claim first.

Cost if wrong: one file in `lib/`, and a move if the name turns out to be wrong. Cost of getting it wrong the other way is the defect this whole task exists to fix, one axis over.

**Ruling — a branded direction-carrying type was considered and REJECTED; do not re-propose it.** A branded `InboundBranches` type, or splitting `branches` into direction-tagged shapes, would make the fan-in inversion *unwriteable* rather than merely unlikely — it is the only intervention that addresses the root cause rather than the recurrence. It is still not worth it here: it touches `types/index.ts`, `guidedDecoder.ts:2055-2062`, `test/composerFixtures.ts` and every composition fixture in the frontend suite, for a rule that one shared, tested, imported helper already protects in practice. Recorded so the next reviewer does not spend the analysis again. Cost if wrong: the rule stays enforced by a helper and its tests rather than by the type system, so someone who bypasses the helper can still write the inversion — which is why the helper is a LEAF module with an authority-naming header rather than something easy to walk past.

**Ruling — the existing GraphView coverage STAYS in `GraphView.test.tsx`; the new tests ADD unit coverage, they do not replace it.** The topology cases already in that file — notably `describe("coalesce correlated branch fan-in (elspeth-625e85c59b)")` at `:2018` and the non-terminal-coalesce test at `:2121` (`:2123` is inside its comment), which cites `_producer_resolver.published_success_connection` by name — are **integration** tests: they render `GraphView` and assert on the edges it draws. They cannot be relocated into a leaf-module unit test without losing what they check, and they are precisely the evidence that this relocation changed no behaviour. So: do not move them, do not touch them, and treat any change in their result as a failed relocation. The new `graphTopology.test.ts` adds what did not exist before — direct unit coverage of the rules themselves, which today are only reachable through a 2004-line component. Cost if wrong: two test files cover overlapping ground, which for a rule with two production incidents behind it is the right side to err on.

**Ruling — the doctrine comments move BYTE-IDENTICAL, with TWO named exceptions and no others.** They are the memory of two production defects and the reason each rule is shaped the way it is; a paraphrase loses precisely the detail that would stop the next person re-deriving them. The reviewer's check is `git diff` showing pure relocation. Cost if wrong: the extraction preserves the code and discards the reason for it — the worse half.

**The exact layout, because the reviewer diffs against these ranges and a pre-fix draft got them wrong** (it said `:86-135` and `:158-181`; `:158` is `]);`, the close of the `FAN_IN_NODE_TYPES` declaration, `:159` is blank, and `:175-181` is `branchEntries`'s signature and body — the second range started and ended on CODE):

| Lines | Content |
|---|---|
| `:86-112` | `/** … */` JSDoc describing `branches`/INBOUND semantics — i.e. it documents **`FAN_IN_NODE_TYPES`**, which is declared 43 lines below at `:155-158`. See exception (a). |
| `:113-135` | `// …` block for `IMPLICIT_SELF_PUBLISHING_NODE_TYPES`, mirroring `_producer_resolver.published_success_connection` |
| `:136-140` | `IMPLICIT_SELF_PUBLISHING_NODE_TYPES` declaration |
| `:142-153` | `publishedSuccessConnection` (`:153` is the closing `}`) |
| `:155-158` | `FAN_IN_NODE_TYPES` declaration (`:158` is `]);`) |
| `:160-174` | `/** … */` JSDoc for `branchEntries` |
| `:175-182` | `branchEntries` (`:182` is the closing `}`) |

So the three comment blocks are **`:86-112`, `:113-135` and `:160-174`**. Do not summarise, re-word, re-wrap or "tidy" any of them, except:

**Exception (a) — the `:86-112` JSDoc is REPAIRED IN PLACE by re-placing it, and this is the one reordering the purity check permits.** That block opens *"Node kinds whose INBOUND topology is declared by `branches`…"*, spends eleven lines on list-vs-map `branches` semantics, cites `elspeth-625e85c59b`, and closes *"`branchEntries` below applies the rule rather than assuming it away"* and *"This set governs INBOUND inference only."* It unambiguously documents `FAN_IN_NODE_TYPES`. It is physically attached to `IMPLICIT_SELF_PUBLISHING_NODE_TYPES` (`:136-140`) — a different set, about OUTBOUND publishing, which has its own block at `:113-135`. This misplacement is pre-existing in the tree, not something this task introduces; but "move byte-identical" plus "no reordered declaration" would otherwise ship a leaf module whose lead docblock describes the wrong constant, in a module whose entire justification is discoverability. **So: move the `:86-112` block byte-identical in its TEXT, and place it directly above `FAN_IN_NODE_TYPES` in the new module.** It is called out here so the reviewer expects exactly one block to appear in a different position than the source order, and treats any *other* reordering as a failed relocation.

**Exception (b) — cross-file pointers are re-pointed, because a verbatim move makes them false.** `GraphView.tsx:110-111`, inside the block exception (a) moves, reads:

```
 * This set governs INBOUND inference only. The outbound-semantics rewrite
 * below stays row_union-scoped on purpose — see the comment there.
```

"below" and "there" resolve to `GraphView.tsx:1670` and `:1857`, both of which **stay in `GraphView.tsx`** (this task leaves those two comment mentions unchanged). After a byte-identical move, `lib/graphTopology.ts` would contain a pointer to a "comment below" that is not in that file, with no way to find it. That is the same defect as naming constraint 2 below — *"a wrong path in a header comment is worse than none, because it teaches the next reader that the mirror has no authority"* — arriving by a different route. The licensed replacement, quoted here in full so the reviewer's diff check knows exactly which two lines may change and what they must become:

```
 * This set governs INBOUND inference only. The outbound-semantics rewrite
 * stays row_union-scoped on purpose — see the comment at
 * components/inspector/GraphView.tsx, above `authoritativeRowUnionOutboundSemantics`.
```

Nothing else in any of the three blocks may change by a single byte. Cost if wrong: a reviewer rejects a correct relocation because the diff shows two changed comment lines, or accepts a paraphrase because the rule had no carve-out and the lane invented one.

**Ruling — the coalesce member sets move here too, and become tuples so the phrase maps can close against them.** `COALESCE_POLICIES` and `COALESCE_MERGES` already exist in the frontend, private, at `api/guidedDecoder.ts:75-76`. Task 4's `POLICY_PHRASES` / `MERGE_PHRASES` would otherwise be the **third** statement of those member sets — second in the frontend — after `core/config.py:1007` and `:1011`. That is the same archetype as the topology bug, caught before it landed rather than after, so it gets the same remedy in the same commit. Two details make this cheap and safe:

- They belong in `graphTopology.ts` rather than a new module: they are coalesce fan-in semantics, which is what this module is about, and the alternative (exporting them from a 2289-line decoder) makes a display module import the validation layer.
- They change shape from `Set<string>` to `as const` tuple + derived union type. `guidedDecoder.ts` keeps a `Set` by constructing one from the tuple, so its validation code is untouched; Task 4 gets `Record<CoalescePolicy, string>`, making **a member added to the tuple without a phrase** a compile error instead of silently degrading to title-cased machine text. (Backend→frontend drift is a separate guard — Step 2b's parity assertion. See the phrase-map ruling in Task 4 for why the two must not be confused.)

`types/index.ts:180-181` still types `policy`/`merge` as `string | null`, so the runtime `?? titleCaseLabel(value)` fallback in `routingPhrase` stays and is still correct — this closes the map at compile time without narrowing the wire type, which is a wider change (see the Roadmap tail). Cost if wrong: `guidedDecoder.ts` gains one import line for a set it used to declare inline.

- [ ] **Step 1: Write the failing topology tests**

`src/lib/graphTopology.test.ts` — **four** describes, covering everything this module will own. The first two pin the leaf rules that were previously provable only through a 2004-line component; the third pins the producer-registration rule lifted from `GraphView.tsx:1245-1260` + `:1298-1351`, including the multimap discipline and the two sentinel divergences; the fourth pins what the module gains from elsewhere — the coalesce member sets (private in `api/guidedDecoder.ts` until now) and the `discard` sentinel, which had no named home at all. Do not stop after two:

```ts
import { describe, expect, it } from "vitest";

import {
  branchEntries,
  buildConnectionProducers,
  publishedSuccessConnection,
  COALESCE_MERGES,
  COALESCE_POLICIES,
  DISCARD_CONNECTION,
  FAN_IN_NODE_TYPES,
  IMPLICIT_SELF_PUBLISHING_NODE_TYPES,
} from "./graphTopology";
import { makeComposition } from "@/test/composerFixtures";

describe("publishedSuccessConnection", () => {
  it("prefers an explicit on_success over the implicit self-published id", () => {
    expect(
      publishedSuccessConnection({ id: "merge", node_type: "coalesce", on_success: "tidy_output" }),
    ).toBe("tidy_output");
  });

  it("publishes under the node's own id for queue, coalesce and aggregation with no on_success", () => {
    // Mirrors _producer_resolver.published_success_connection. Re-deriving
    // this from on_success alone drew a working fork/coalesce pipeline as two
    // disconnected fragments (session 3f02c8fa).
    for (const node_type of ["queue", "coalesce", "aggregation"]) {
      expect(publishedSuccessConnection({ id: "n1", node_type, on_success: null })).toBe("n1");
    }
    expect(IMPLICIT_SELF_PUBLISHING_NODE_TYPES.has("row_union")).toBe(false);
    expect(IMPLICIT_SELF_PUBLISHING_NODE_TYPES.has("collector")).toBe(false);
  });

  it("publishes nothing for a kind that requires on_success and declares none", () => {
    expect(publishedSuccessConnection({ id: "u1", node_type: "row_union", on_success: null })).toBeNull();
    expect(publishedSuccessConnection({ id: "t1", node_type: "transform", on_success: null })).toBeNull();
  });
});

describe("branchEntries", () => {
  it("reads a map verbatim and expands a list to the identity mapping", () => {
    // A coalesce reaches the frontend still holding a LIST: the composer
    // normalises list -> identity only for row_union (_serialize_branches
    // preserves list-vs-mapping for a coalesce). Declining the list shape
    // reproduced elspeth-625e85c59b on a composition that validates green.
    expect(branchEntries({ branch_a: "pairing_done", branch_b: "hex_done" })).toEqual([
      ["branch_a", "pairing_done"],
      ["branch_b", "hex_done"],
    ]);
    expect(branchEntries(["a", "b"])).toEqual([
      ["a", "a"],
      ["b", "b"],
    ]);
    expect(branchEntries(null)).toEqual([]);
    expect(branchEntries(undefined)).toEqual([]);
  });

  it("covers both fan-in kinds", () => {
    expect([...FAN_IN_NODE_TYPES].sort()).toEqual(["coalesce", "row_union"]);
  });
});

describe("buildConnectionProducers", () => {
  it("registers a source's on_success, a node's published connection, on_error and routes", () => {
    const state = makeComposition(1, {
      sources: { intake: { plugin: "csv", options: {}, on_success: "raw_rows" } },
      nodes: [
        { id: "classify", node_type: "transform", plugin: "llm", options: {},
          input: "raw_rows", on_success: "scored", on_error: "review_queue" },
        { id: "route", node_type: "gate", plugin: null, options: {},
          input: "scored", on_success: null, on_error: null, routes: { pass: "kept", fail: "dropped" } },
      ],
      // `kept` is consumed by an output NAMED for it — OutputSpec has no
      // `input` field (types/index.ts:204-211), and an output consumes the
      // connection equal to its own name.
      outputs: [{ name: "kept", plugin: "csv", options: {} }],
    });
    const producers = buildConnectionProducers(state);
    expect(producers.get("raw_rows")).toEqual(["intake"]);
    expect(producers.get("scored")).toEqual(["classify"]);
    expect(producers.get("review_queue")).toEqual(["classify"]);
    expect(producers.get("kept")).toEqual(["route"]);
    expect(producers.get("dropped")).toEqual(["route"]);
    // Note the fixture's shape: every node literal carries `on_error`, which
    // NodeSpec declares REQUIRED (types/index.ts:174, `on_error: string | null`
    // with no `?`). Omitting it is TS2741 and `npx tsc --noEmit -p
    // tsconfig.test.json` — a gate this plan requires clean at Task 3 Step 3,
    // Task 4 Step 4 and Task 11 Step 1a — would go red on the plan's own code.
  });

  it("is a MULTIMAP — several producers on one connection all survive (ADR-028 fan-in)", () => {
    // GraphView.tsx:1246-1249: overwriting would silently drop every producer
    // but the last and misrender the intentional fan-in.
    const state = makeComposition(1, {
      sources: { a: { plugin: "csv", options: {}, on_success: "pooled" } },
      nodes: [
        { id: "b", node_type: "transform", plugin: "llm", options: {},
          input: "seed", on_success: "pooled", on_error: null },
        { id: "hold", node_type: "queue", plugin: null, options: {},
          input: "pooled", on_success: null, on_error: null },
      ],
      outputs: [],
    });
    expect(buildConnectionProducers(state).get("pooled")?.sort()).toEqual(["a", "b"]);
  });

  it("publishes a queue under its own id and does NOT register the 'fork' route sentinel", () => {
    const state = makeComposition(1, {
      sources: {},
      nodes: [
        { id: "hold", node_type: "queue", plugin: null, options: {},
          input: "pooled", on_success: null, on_error: null },
        { id: "split", node_type: "gate", plugin: null, options: {}, input: "hold",
          on_success: null, on_error: null, routes: { every: "fork" }, fork_to: ["arm_a", "arm_b"] },
      ],
      outputs: [],
    });
    const producers = buildConnectionProducers(state);
    expect(producers.get("hold")).toEqual(["hold"]);
    expect(producers.get("arm_a")).toEqual(["split"]);
    expect(producers.get("arm_b")).toEqual(["split"]);
    // "fork" is a sentinel, not a connection name. GraphView registers it and
    // never looks it up; this reduced view skips it so it cannot surface in
    // the Spec tab as a resolvable connection (see the producer-registry ruling).
    expect(producers.has("fork")).toBe(false);
  });

  it("never registers the discard sentinel as a connection", () => {
    // _producer_resolver.py:208 — discard is not a connection. A shared index
    // holding it as a key would let some component's `input: discard` resolve
    // to a producer.
    const state = makeComposition(1, {
      sources: { source: { plugin: "csv", options: {}, on_success: "raw_rows", on_validation_failure: "discard" } },
      nodes: [{ id: "score", node_type: "transform", plugin: "llm", options: {},
                input: "raw_rows", on_success: "final_out", on_error: "discard" }],
      outputs: [{ name: "final_out", plugin: "csv", options: {} }],
    });
    expect(buildConnectionProducers(state).has(DISCARD_CONNECTION)).toBe(false);
  });
});

describe("shared member sets and sentinels", () => {
  it("pins the frontend's single copy of the coalesce members", () => {
    // Backend authority: core/config.py:1007 and :1011. These were declared
    // privately a SECOND time in api/guidedDecoder.ts:75-76; this module is
    // now the one place the frontend states them.
    //
    // Be honest about what this assertion is: it compares a TypeScript literal
    // against another TypeScript literal, so it is DRIFT DETECTION within the
    // frontend — it can only fail when someone edits the tuple. It is NOT the
    // cross-language mirror. That is the parity assertion Step 2b adds to
    // tests/unit/web/composer/test_graph_topology_parity.py, which
    // reads this file and compares against the Python Literals.
    expect([...COALESCE_POLICIES]).toEqual(["require_all", "quorum", "best_effort", "first"]);
    expect([...COALESCE_MERGES]).toEqual(["union", "nested", "select"]);
  });

  it("names the discard sentinel so PipelineSpecView and SchemaFormTurn stop spelling it", () => {
    // _producer_resolver.py:208 — discard is not a connection. The two
    // production sites this replaces are PipelineSpecView.tsx:52 and
    // SchemaFormTurn.tsx:74. This assertion alone is a tautology; what makes
    // the constant load-bearing is that both callers import it, which the
    // Task 4 tests and the SchemaFormTurn suite exercise.
    expect(DISCARD_CONNECTION).toBe("discard");
  });
});
```

Run: `npx vitest run src/lib/graphTopology.test.ts` → FAIL (module missing).

- [ ] **Step 2: Move the four helpers, the two member sets, and name the sentinel**

Create `src/lib/graphTopology.ts` with this header, then **cut** `GraphView.tsx:86-182` — the three doctrine comment blocks (`:86-112`, `:113-135`, `:160-174`) and the four declarations — and paste them below it, adding only the `export` keyword to each of the four. **`:182` is the closing `}` of `branchEntries`: cutting `:86-181` leaves an orphan brace in `GraphView.tsx` and an unterminated function body in the new module, and neither file parses.** Two text changes are licensed and only two: the `:86-112` block is placed above `FAN_IN_NODE_TYPES` rather than above `IMPLICIT_SELF_PUBLISHING_NODE_TYPES`, and its two-line "see the comment there" pointer is re-pointed — both quoted verbatim in the byte-identical ruling above. Everything else is byte-for-byte.

```ts
// ============================================================================
// graphTopology — THE frontend's single model of how composition components
// join up: what a node publishes, what a fan-in node reads, and what is not
// a connection at all. Every surface that needs to answer "which component is
// on the other end of this connection?" imports from here. If you are about
// to write a second one, this is the one you were looking for.
//
// Deliberately a LEAF module — it imports only types and the type-only
// utils/compositionState.ts — so the Graph tab
// (components/inspector/GraphView.tsx) and the Spec tab
// (components/workspace/specRouting.ts) can both import it with no cycle and
// no React in the dependency graph. Same contract as lib/validationHumaniser.ts
// and components/chat/guided/stepLabels.ts, and it exists for the same reason
// stepLabels.ts does: hand-mirrored copies of one rule drift
// (elspeth-93f5621f18, Wave 3).
//
// Every rule here mirrors a NAMED backend authority, by path, so that a
// `git grep _producer_resolver` or `git grep connection_consumers` run from
// the Python side finds this mirror. Do not re-derive one from the wire shape:
//
//   * src/elspeth/web/composer/_producer_resolver.py:98
//     `published_success_connection(node)` decides what a node publishes:
//     on_success if set, else the node id for queue/coalesce/aggregation,
//     else nothing. `publishedSuccessConnection` below is its mirror.
//     Also :208 — `if connection_name is None or connection_name == "discard"`
//     is the statement that `discard` is a sentinel, not a connection;
//     DISCARD_CONNECTION below is that literal, named.
//   * src/elspeth/web/composer/guided/connection_consumers.py:31-40
//     the canonical consumer projection: it registers each BRANCH connection
//     as consumed by the coalesce/row_union and then `continue`s, never
//     registering `node.input` for a fan-in node — that scalar is only the
//     backend-compatible first-branch placeholder.
//   * src/elspeth/core/config.py:984-986
//     `CoalesceSettings.branches` is a "Branch identity -> INPUT connection
//     mapping". A fan-in node's branches are what it READS, never what it
//     publishes. Reading them as outbound makes a coalesce name ITSELF,
//     because its own `input` is one of its branch connections.
//   * src/elspeth/core/config.py:1007 and :1011
//     the coalesce policy and merge Literals, mirrored below as tuples.
//
// The comments on each declaration are the record of the two incidents that
// produced these rules (session 3f02c8fa; elspeth-625e85c59b). They moved
// here verbatim and must stay that way.
// ============================================================================

import type { NodeSpec } from "@/types/index";

/**
 * Not a connection: the backend's sentinel for "drop this, and record the
 * drop in the audit trail" (_producer_resolver.py:208 refuses to register a
 * producer for it). Named here because two frontend sites spelled it as a
 * bare literal and nothing tied them to that rule:
 * components/workspace/PipelineSpecView.tsx:52 and
 * components/chat/guided/SchemaFormTurn.tsx:74.
 *
 * NOT the same word as `ProposalEndpointKind`'s "discard"
 * (api/guidedDecoder.ts:250, types/guided.ts:600). That is a guided-proposal
 * ENDPOINT KIND — a discriminated-union literal whose narrowing REQUIRES the
 * literal in the type position, so this constant cannot and must not replace
 * it. Two vocabularies, one word. Do not merge them.
 */
export const DISCARD_CONNECTION = "discard";

/**
 * The coalesce member sets, mirrored from core/config.py:1007 and :1011 and
 * lifted out of api/guidedDecoder.ts:75-76, which held the frontend's only
 * copy privately.
 *
 * `as const` so consumers get a union type. Be precise about what that buys:
 * a display map keyed `Record<CoalescePolicy, string>` fails the BUILD when
 * a member is added to or removed from THIS TUPLE without a phrase. It does
 * NOT fail when core/config.py gains a member — `tsc` cannot see Python.
 * What catches that is the parity assertion in
 * tests/unit/web/composer/test_graph_topology_parity.py, which
 * regexes these two tuples and compares them against the Literals. Both
 * halves are needed; neither is the other.
 */
export const COALESCE_POLICIES = ["require_all", "quorum", "best_effort", "first"] as const;
export type CoalescePolicy = (typeof COALESCE_POLICIES)[number];

export const COALESCE_MERGES = ["union", "nested", "select"] as const;
export type CoalesceMerge = (typeof COALESCE_MERGES)[number];
```

Type the two functions structurally so both `NodeSpec` and `GraphView`'s local node shape satisfy them without a cast — `publishedSuccessConnection` already takes an inline `{ id; node_type; on_success }` shape, which is exactly right and stays; `branchEntries` already takes `string[] | Record<string, string> | null | undefined`, which is `NodeSpec["branches"]`. (The `NodeSpec` import is for the doc reference only. If it is flagged as unused, **`npm run lint` (eslint) is what will flag it, not `tsc --noEmit`** — an unused *type* import is not a `tsc` error under this project's configs, so running the typecheck and seeing it clean proves nothing. Drop the import rather than inventing a use.)

Then in `GraphView.tsx` add the import beside the existing `@/utils` imports:

```ts
import {
  branchEntries,
  publishedSuccessConnection,
  FAN_IN_NODE_TYPES,
} from "@/lib/graphTopology";
```

`IMPLICIT_SELF_PUBLISHING_NODE_TYPES` is referenced only by `publishedSuccessConnection`, so it does not need importing into `GraphView.tsx`. The three call sites (`:1313`, `:1365`, `:1367`) and the two comment mentions at `:1670` and `:1857` are unchanged — those comments still name `FAN_IN_NODE_TYPES` correctly, it just lives elsewhere now.

Finally, in `api/guidedDecoder.ts` replace the two inline declarations at `:75-76` with a `Set` built from the shared tuples, so every one of that file's existing membership checks keeps working unchanged:

```ts
import { COALESCE_MERGES, COALESCE_POLICIES } from "@/lib/graphTopology";

const COALESCE_POLICY_SET = new Set<string>(COALESCE_POLICIES);
const COALESCE_MERGE_SET = new Set<string>(COALESCE_MERGES);
```

and rename the two identifiers at their use sites (`git grep -n "COALESCE_POLICIES\|COALESCE_MERGES" -- src/api` finds them; there are only the declarations plus their membership checks). Nothing else in that 2289-line file changes, and `guidedDecoder.test.ts` must stay green untouched. If you would rather not rename, keep the local names and construct the sets under them — the requirement is that the MEMBERS come from `graphTopology`, not that the local identifiers change.

- [ ] **Step 2b: Re-anchor the Python parity gate and extend it to the two ungated mirrors**

**This step is why Task 3 is a backend-touching task. Do not skip it: without it the branch goes red for every sibling lane the moment Step 2 lands, and nothing in Task 3's frontend acceptance can see it.** Run from the repository root with `source .venv/bin/activate`.

Edit `tests/unit/web/composer/test_graph_view_self_publishing_parity.py`:

1. **Rename the module** to `tests/unit/web/composer/test_graph_topology_parity.py` (`git mv`). The old name names a file that no longer holds the declarations.

   **Blast radius of the rename, measured from the REPOSITORY ROOT** (the same mistake this task exists to correct must not be repeated on its own remedy): `git grep -n 'test_graph_view_self_publishing_parity' -- . ':!docs/plans/2026-08-30-composer-detail-level-wave3.md'` returns **nothing** — no CI config, no docs page, no other test's docstring, no skill references the module by name. So the rename touches exactly one path. **Re-run that grep before renaming**; if it returns anything, either add those files to this task's Files list or **drop the rename entirely** — it is cosmetic, the re-anchor is the load-bearing half, and a slightly stale filename costs less than an unmeasured rename.
2. **Re-anchor the path constant** (`:41-42`), renaming it since it no longer points at `GraphView.tsx`:

```python
_PACKAGE_ROOT = Path(elspeth.__file__).parent
_TOPOLOGY_PATH = _PACKAGE_ROOT / "web" / "frontend" / "src" / "lib" / "graphTopology.ts"
```

3. **Replace every remaining `_GRAPH_VIEW_PATH` reference** with `_TOPOLOGY_PATH` — there are eight (`:56`, `:60`, `:76`, `:81`, `:96`, `:98`, `:103`, `:112`, `:114`), and six of them are inside assertion messages that name `GraphView.tsx` by hand. `_TOPOLOGY_PATH.name` renders `graphTopology.ts`, so the interpolated messages self-correct; the two that spell the filename in prose do not. Per this task's own naming constraint 2 — *a wrong path in a header comment is worse than none* — leaving a message that sends a future reader to `GraphView.tsx` is the same defect the constraint forbids.
4. **Update the module docstring** (`:1-31`): `` ``GraphView.tsx`` restates, in TypeScript, …`` becomes `` ``lib/graphTopology.ts`` restates, in TypeScript, …``, and the instruction at `:22-24` (*"update ``IMPLICIT_SELF_PUBLISHING_NODE_TYPES`` in ``GraphView.tsx`` in the same commit"*) names the new home. Keep the two incident paragraphs (`:15-20`) byte-identical — they are the same memory this task's own comment ruling protects.
5. **The three existing regexes and assertions need no other change.** `_SET_DECLARATION_RE` (`:48-50`) is not line-anchored, so `const IMPLICIT_SELF_PUBLISHING_NODE_TYPES: ReadonlySet<string> = new Set([…]);` still matches inside `export const …`. `"function publishedSuccessConnection("` (`:97`) still matches after `export function …`. `"IMPLICIT_SELF_PUBLISHING_NODE_TYPES.has(node.node_type)"` (`:102`) is inside the moved function body and moves with it.
6. **Add two parity assertions, closing the two mirrors that had no gate** (the mirror table above). Model them on the existing one: regex the TS literal, compare against the Python authority, carry a vacuity smoke so a regex that matches nothing fails loudly.

```python
from elspeth.core.config import CoalesceSettings
from elspeth.web.composer.guided.connection_consumers import _coalesce_branch_connections  # noqa: F401  (import proves the module path in the docstring resolves)

_FAN_IN_DECLARATION_RE = re.compile(
    r"const\s+FAN_IN_NODE_TYPES\s*:\s*ReadonlySet<string>\s*=\s*new\s+Set\(\s*\[(?P<body>[^\]]*)\]\s*\)\s*;",
)
_TUPLE_RE_TEMPLATE = r"const\s+{name}\s*=\s*\[(?P<body>[^\]]*)\]\s*as\s+const\s*;"


def _ts_members(pattern: re.Pattern[str], label: str) -> list[str]:
    text = _TOPOLOGY_PATH.read_text(encoding="utf-8")
    matches = pattern.findall(text)
    assert len(matches) == 1, (
        f"Expected exactly one `{label}` declaration in {_TOPOLOGY_PATH.name}, matched {len(matches)}. "
        "The declaration moved, was renamed, or Prettier rewrote its shape — re-anchor this regex "
        "rather than deleting the parity assertion, which is the only thing pinning the TS copy to "
        "the Python authority."
    )
    return _MEMBER_RE.findall(matches[0])


def test_fan_in_node_types_matches_the_canonical_consumer_projection() -> None:
    """`FAN_IN_NODE_TYPES` mirrors the arm in `connection_consumers.py:32`.

    That arm is the Python authority for "which node kinds declare their
    inbound wiring through `branches` rather than through the scalar `input`".
    Getting it wrong drew a coalesce with a single arm on a composition that
    validates green (elspeth-625e85c59b).
    """
    ts_kinds = set(_ts_members(_FAN_IN_DECLARATION_RE, "FAN_IN_NODE_TYPES"))
    assert ts_kinds, f"No members parsed from FAN_IN_NODE_TYPES in {_TOPOLOGY_PATH} — the assertion would be vacuous."
    assert ts_kinds == {"coalesce", "row_union"}, (
        f"FAN_IN_NODE_TYPES in {_TOPOLOGY_PATH.name} is {sorted(ts_kinds)}, but "
        "`connection_consumers.py`'s canonical consumer projection treats exactly "
        "('coalesce', 'row_union') as branch-wired. A kind in one list and not the other means the "
        "Spec tab and the Graph tab infer a different inbound topology than the runtime builds."
    )


def test_coalesce_member_tuples_match_the_backend_literals() -> None:
    """`COALESCE_POLICIES` / `COALESCE_MERGES` mirror `CoalesceSettings`'s Literals.

    The frontend closes its display maps against these tuples, so an unphrased
    member is a compile error THERE — but only this test can see a member added
    on the PYTHON side. Without it the new value degrades silently to
    title-cased machine text at the user.
    """
    py_policies = set(get_args(CoalesceSettings.model_fields["policy"].annotation))
    py_merges = set(get_args(CoalesceSettings.model_fields["merge"].annotation))
    for label, py_members in (("COALESCE_POLICIES", py_policies), ("COALESCE_MERGES", py_merges)):
        ts_members = set(
            _ts_members(re.compile(_TUPLE_RE_TEMPLATE.format(name=label)), label)
        )
        assert ts_members, f"No members parsed from {label} in {_TOPOLOGY_PATH} — the assertion would be vacuous."
        assert ts_members == py_members, (
            f"{label} in {_TOPOLOGY_PATH.name} is {sorted(ts_members)}; `CoalesceSettings` declares "
            f"{sorted(py_members)}. Add the missing member to the tuple AND write its phrase in "
            "components/workspace/specRouting.ts, which closes a Record against it."
        )
```

**Checked against the live tree, so the snippet is not guesswork — including the CLASS NAME, which a pre-fix draft got wrong.** The class is **`CoalesceSettings`** (`src/elspeth/core/config.py:944`, `class CoalesceSettings(BaseModel)`); there is no `CoalesceSpec` anywhere in this tree (`git grep -n "CoalesceSpec" -- src tests` → no output). That mattered more than an ordinary name slip: the `from elspeth.core.config import CoalesceSettings` line in the snippet above is a **module-level import**, so a wrong class name fails `test_graph_topology_parity.py` at **collection** — all five tests error, including the three re-anchored ones — and Task 3 Step 3's `pytest` command goes red for a reason unrelated to the relocation, which is the worst possible signal at that gate. The code being moved had it right all along (`GraphView.tsx:87-100` says `CoalesceSettings.normalize_branches` and `CoalesceSettings.on_success`), so the wrong name propagated out of prose and into executable code. `core/config.py:1007` declares `policy: Literal["require_all", "quorum", "best_effort", "first"] = Field(default="require_all", …)` and `:1011` declares `merge: Literal["union", "nested", "select"] = Field(default="union", …)` — a Pydantic default does **not** wrap the annotation in `Optional`, so `CoalesceSettings.model_fields["policy"].annotation` is the bare `Literal` and `get_args` returns the four strings directly. Import `get_args` from `typing`. Do not add a `getattr`/`hasattr` anywhere in this file (whole-tree gate); `model_fields[...]` is a dict subscript and `.annotation` is direct attribute access on a type ELSPETH's dependency owns — both fine under ADR-032's nominal arm.

Run: `pytest tests/unit/web/composer/test_graph_topology_parity.py -q` → **3 existing tests + 2 new = 5 PASS.** Then deliberately break it once to prove it is not vacuous: temporarily change `"first"` to `"firsts"` in the TS tuple, re-run, confirm `test_coalesce_member_tuples_match_the_backend_literals` FAILS, revert. Record that in the commit message.

- [ ] **Step 3: Prove it is a pure relocation**

```bash
# frontend, from src/elspeth/web/frontend
npx vitest run src/lib/graphTopology.test.ts src/components/inspector src/api   # PASS; GraphView.test.tsx and guidedDecoder.test.ts unchanged
npx tsc --noEmit -p tsconfig.app.json && npx tsc --noEmit -p tsconfig.test.json   # clean
git diff -- src/elspeth/web/frontend/src/components/inspector/GraphView.tsx      # deletions + one import block ONLY
git diff -- src/elspeth/web/frontend/src/api/guidedDecoder.ts                    # two declarations + one import ONLY

# backend, from the repository root with the venv active — the ONLY whole-tree
# proof that the relocation was behaviour-preserving. The two vitest suites
# above cannot see it, which is exactly how a pre-fix draft of this plan
# certified Task 3 green while leaving the branch red.
pytest tests/unit/web/composer/test_graph_topology_parity.py \
       tests/unit/web/composer/test_producer_resolver.py -q                      # PASS
```

The `git diff` on `GraphView.tsx` must show exactly **FOUR** things and nothing else:

1. the removed helper lines (`:86-182`);
2. the added import;
3. `:1245-1260` and `:1298-1351` hoisted verbatim into the exported module-level `buildProducerRegistry(state)`, which ends `return connectionProducers;` — **the two blocks move byte-identical; only their enclosing scope changes, and `:1262-1296` stays behind**;
4. the `useMemo`'s **rebinding** line, `const connectionProducers = buildProducerRegistry(compositionState);`, where the old declaration used to be.

**A pre-fix draft said "exactly three things" and named only `:1298-1350` — a rule that FAILS a correct implementation**, because every correct hoist must also move `:1245-1260` and must rebind the return value. If the diff shows a fifth thing, or if `:1262-1296` moved, the relocation was not pure. No re-worded comment beyond the two lines exception (b) licenses, no reordered declaration beyond the one exception (a) licenses, no changed signature. The diff on `guidedDecoder.ts` must be confined to `:75-76` and the import block. If either shows more, the relocation was not pure; revert and redo it. **Neither `GraphView.test.tsx` nor `guidedDecoder.test.ts` may appear in `git status` at all** — those two staying green and untouched is the entire proof that this task changed no behaviour, and `GraphView.test.tsx:2018` (`describe("coalesce correlated branch fan-in (elspeth-625e85c59b)")`) / `:2121` (the non-terminal-coalesce test; `:2123` is inside its comment) in particular are the cases that pin the two rules being moved. `test_graph_topology_parity.py` **is** expected in `git status` — it is a renamed, re-anchored and extended file, and its absence from the diff means Step 2b was skipped.

- [ ] **Step 4: Commit**

**Run from the REPOSITORY ROOT — SIX pathspecs, not four.** A pre-fix draft staged only the four frontend files, which is worse than a plain omission for the parity test: **`git mv` stages the rename immediately, carrying the OLD file content.** The sequence a lane actually gets is `git mv` (rename staged, body still anchored at `GraphView.tsx`) → Step 2b's content edits sit UNSTAGED → `git add <four frontend paths>` → `git commit`. The commit then contains a file named `test_graph_topology_parity.py` whose body still points at `GraphView.tsx`, so all three of its original tests fail on the moved symbols — the exact breakage this task exists to prevent — while the diff *looks* like Step 2b was done. Step 3 runs before Step 4, so the lane sees green and then commits without the file that made it green.

```bash
git add src/elspeth/web/frontend/src/lib/graphTopology.ts \
        src/elspeth/web/frontend/src/lib/graphTopology.test.ts \
        src/elspeth/web/frontend/src/components/inspector/GraphView.tsx \
        src/elspeth/web/frontend/src/api/guidedDecoder.ts \
        src/elspeth/web/frontend/src/components/chat/guided/SchemaFormTurn.tsx \
        tests/unit/web/composer/test_graph_topology_parity.py
git commit -m "refactor(topology): lift the connection-topology rules and coalesce member sets into lib/graphTopology — one model for the graph, the Spec tab and the decoder (elspeth-93f5621f18)"
git status --short   # MUST show no leftover tests/unit/web/composer/ or SchemaFormTurn entry
```

`SchemaFormTurn.tsx` is the `DISCARD_CONNECTION` conversion this task's own ruling assigns to it ("one line, in Task 3's commit"); leaving it unstaged strands an edit in a shared checkout, which is the hazard the Global Constraints forbid. **The `git status` line is the check, not a formality:** a leftover `tests/unit/web/composer/` entry means the parity edits missed the commit.

No ticket closes here; this is the mechanism half of `elspeth-93f5621f18` part B and is named in that ticket's closeout comment (Task 11).

---

### Task 4: Spec tab — routing values and headings in the reader register (`elspeth-93f5621f18`, part B)

**Files:**
- Create: `src/elspeth/web/frontend/src/components/workspace/specRouting.ts`
- Create: `src/elspeth/web/frontend/src/components/workspace/specRouting.test.ts`
- Modify: `src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.tsx` (`SpecRow` `:7-15`, `routingLabel`/`routingValue` `:47-69`, `SpecSection` `:71-134`, row builders `:136-220`)
- Modify: `src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.test.tsx` — **the three test boundaries are `:192-249` (coalesce fan-in), `:251-292` (branch-map prose) and `:294-355` (collector).** A pre-fix draft of this line wrote `:192-294` and `:294-357`, which silently span two tests each and run into the next one; Step 3's per-item ranges are the correct ones and this header now agrees with them.
- **Also modified, and easy to miss:** `PipelineSpecView.test.tsx:157` and `:541` pin `on_write_failure: "discard"` / `on_validation_failure: "discard"` in fixtures OUTSIDE all three ranges above. They must keep rendering `"dropped (recorded in the audit trail)"` — see the routing-value preservation ruling. If they go red, `RoutingDd` is testing `DISCARD_CONNECTION` too late.

**Interfaces:**
- Consumes: `buildConnectionProducers`, `publishedSuccessConnection`, `branchEntries`, `FAN_IN_NODE_TYPES`, `DISCARD_CONNECTION`, `COALESCE_POLICIES`/`COALESCE_MERGES` (Task 3); `stepLabelForNodeId`, `isComponentPresent` (Task 1); `titleCaseLabel` (`components/catalog/pluginDisplayName.ts`); `sortedSourceEntries`; `expectNoIdentifiersInDefaultDom` with `allowAriaLabelSelectors` (Task 2). **Both upstream tasks are HARD dependencies at compile time, not soft ones** — without Task 2 the exemption argument is a TypeScript error, and without Task 3 the import does not resolve. Step 3 repeats this inline, because a lane that picks up Task 4 alone gets a type error rather than a clear "Task 2 first".
- Produces (all from `specRouting.ts`):
  - `interface RoutingPhrase { text: string; raw: string }`
  - `buildConnectionIndex(state: CompositionState): ConnectionIndex` — `{ consumers: Map<string, string[]>; producers: Map<string, string[]> }` keyed by connection name; values are component ids (node ids, source keys, output names). **The `producers` half is `buildConnectionProducers` from `lib/graphTopology` plus two documented additions; it is NOT re-derived here** — see the index ruling below.
  - `componentPhrase(state, id): string` — description → acronym-aware title case, for a SOURCE, NODE or OUTPUT alike. See the description-rung ruling; it is not simply `stepLabelForNodeId(state, id) ?? titleCaseLabel(id)`, because that ladder consults a description for nodes only.
  - `routingPhrase(state, index, field, value): RoutingPhrase | null` — the whole `<dd>` for one routing field; **null means "fall back to the four rules `routingValue` already owns", NOT `displayValue`** — see the routing-value preservation ruling.
  - `POLICY_PHRASES`, `MERGE_PHRASES`, `OUTPUT_MODE_PHRASES` — **closed `Record`s, not `Map`s** (do not call `.get()` on them): keyed by `CoalescePolicy` / `CoalesceMerge` from `lib/graphTopology` and by `NonNullable<NodeSpec["output_mode"]>` from `types/index.ts:183`, mirroring `core/config.py:1007` `require_all|quorum|best_effort|first` and `:1011` `union|nested|select`. `SCOPE_POLICY_PHRASES` remains a `ReadonlyMap<string, string>` because it has no member set to close against. All four are read through one `ENUM_FIELDS` lookup-function table; unknown values fall to `titleCaseLabel(value)` with the raw in `title`.
- The `<h4>` heading and the `Kind` row (the `elspeth-d74ab492dd` Spec-tab items) are ALSO done here — same file, same pin, same lane — and `elspeth-d74ab492dd`'s closeout comment records that they landed in this PR. Ruling: splitting one 130-line component's copy fix across two PRs so each ticket owns "its" lines would give two reviewers half a pin each; the ticket boundary is recorded in the closeout, not enforced in git. Cost if wrong: one comment on `elspeth-d74ab492dd` pointing at this commit.

**Ruling — connection names resolve through the composition, in the direction the field means.** `on_success: raw_rows` names a CONNECTION; the reader wants the component on the other end. Which end depends on the field, and the two directions are NOT symmetric:

| Field | Direction | Why |
|---|---|---|
| `on_success`, `on_error`, `on_validation_failure`, `on_write_failure`, `fork_to`, `routes` | **downstream** — resolve through `consumers` | the node writes this connection; the reader wants who reads it |
| `input`, `branches` | **upstream** — resolve through `producers` | the node reads this connection; the reader wants who wrote it |

`branches` is the one that is easy to get backwards, and getting it backwards is not a cosmetic slip: because a fan-in node's own `input` is by convention one of its own branch connections, resolving `branches` downstream makes the node resolve to ITSELF — the flagship example renders "Branch Invest Cs1 → **Merge Invest**" instead of "→ Extract Invoice". The `branches`-are-inputs rule is `core/config.py:984-986`; the "skip `node.input` for a fan-in node" rule is `connection_consumers.py:31-40`. Both live in `graphTopology.ts` as of Task 3 and are read here, not restated.

A connection with no resolvable component (dangling mid-edit, or a branch whose producer is not yet wired) falls back to `titleCaseLabel(connection)` — the author named it, so the author-name rule applies. Cost if wrong: a dangling connection reads "Invest Cs1 Done" rather than `invest_cs1_done`; the raw stays in `title`.

**Ruling — the phrase maps close against a member set they do not own; `scope_policy` is the honest exception.** Three of the four enum maps are now keyed by a union type rather than by `string`: `POLICY_PHRASES` and `MERGE_PHRASES` off `CoalescePolicy`/`CoalesceMerge` (Task 3 lifted those members out of `api/guidedDecoder.ts:75-76`, where the frontend already held a private second copy of the `core/config.py:1007,:1011` Literals), and `OUTPUT_MODE_PHRASES` off `NonNullable<NodeSpec["output_mode"]>`, which `types/index.ts:183` already declares as a closed union. The point is the failure mode: with an open `ReadonlyMap<string, string>` plus a `?? titleCaseLabel(value)` fallback, a backend adding a policy member does not break anything — it quietly renders "Someday Maybe"-style prettified machine text at the user, and this task's own test pins that degradation as correct. Closing the maps makes an unphrased member a build error, which is what an unphrased enum should be.

**Be exact about which drift the closure catches, because a pre-fix draft of this ruling was not.** `CoalescePolicy` is derived from `COALESCE_POLICIES`, a TypeScript tuple. Closing `POLICY_PHRASES` against it makes it a **compile error to add a member to that tuple, or to `types/index.ts:183`'s `output_mode` union, without writing a phrase**. It does NOT make a backend addition a build error: a new member in `core/config.py:1007` does not touch the tuple, `tsc` sees nothing, and the value still degrades to `?? titleCaseLabel(value)` at runtime. The backend→frontend half is gated by the parity assertion Task 3 Step 2b adds to `test_graph_topology_parity.py`, which regexes the two tuples out of `lib/graphTopology.ts` and compares them to the Literals. **Compile-time closure and cross-language parity are two different guards and this wave ships both; claiming either does the other's job is the overclaim being corrected here.** `scope_policy` is left OPEN and stays a `Map<string, string>`, because it genuinely has no Literal and no frontend member set to bind to (`types/index.ts:191` types it `string | null`, and a collector's arrival policy is not a coalesce's) — closing a map against a set that does not exist would mean inventing the set here, which is the very thing this wave is correcting. The runtime `titleCaseLabel` fallback stays for all four, because the wire types are still `string | null` and an out-of-union value is representable; the compile-time closure is the guard, the runtime fallback is the graceful degradation behind it. Narrowing `types/index.ts:180-181,191` themselves to the backend Literals is the fuller fix and is parked in the Roadmap tail — it touches the decoder and is wider than this wave. Cost if wrong: a legitimate backend enum addition blocks the frontend build until someone writes one sentence, which is the intended trade.

**Ruling — the `producers` half of the index is CONSUMED from `lib/graphTopology`, not re-derived; the two extra registrations are named here.** A pre-fix draft assembled its own producers map over Task 3's leaf helpers, which is the same defect Task 3 exists to fix moved up one level: the scarred primitives shared, the assembly private. Task 3 now lifts `buildConnectionProducers(state)` out of `GraphView.tsx:1245-1260` + `:1298-1351` — the four registration rules plus the ADR-028 multimap discipline — and this task consumes it:

```ts
export function buildConnectionIndex(state: CompositionState): ConnectionIndex {
  const producers = buildConnectionProducers(state);   // lib/graphTopology — the shared rule
  const consumers = new Map<string, string[]>();
  const push = (map: Map<string, string[]>, key: string, id: string): void => {
    const existing = map.get(key);
    if (existing === undefined) map.set(key, [id]);
    else if (!existing.includes(id)) existing.push(id);
  };

  // Two registrations the Graph tab does NOT make, added HERE rather than
  // inside buildConnectionProducers so the diagram's behaviour cannot change.
  // Verified against GraphView.tsx: `on_validation_failure` appears there only
  // at :628 (a config-panel field, not a producer registration) and
  // `on_write_failure` not at all. The Spec tab needs both because it prints
  // every routing field as prose, including the failure lanes the diagram
  // draws differently or not at all — and leaving them out is ASYMMETRIC: a
  // source's validation-failure lane would resolve upstream while an output's
  // write-failure lane title-cased, for no reason a reader could infer.
  for (const [sourceName, source] of sortedSourceEntries(state)) {
    if (source.on_validation_failure && source.on_validation_failure !== DISCARD_CONNECTION) {
      push(producers, source.on_validation_failure, sourceName);
    }
  }
  for (const output of state.outputs) {
    if (output.on_write_failure && output.on_write_failure !== DISCARD_CONNECTION) {
      push(producers, output.on_write_failure, output.name);
    }
  }

  // Consumers: genuinely new. GraphView has no consumer map — it resolves the
  // consumer direction by looking node.input UP against the producer registry.
  for (const node of state.nodes) {
    for (const connection of nodeInputs(node)) push(consumers, connection, node.id);
  }
  for (const output of state.outputs) {
    if (output.input) push(consumers, output.input, output.name);
    push(consumers, output.name, output.name);   // an output IS its own connection
  }
  return { consumers, producers };
}
```

**The `DISCARD_CONNECTION` guard on both additions is deliberate:** `discard` is not a connection (`_producer_resolver.py:208`), so registering an output that discards write failures as the *producer* of a connection named `discard` would let some other component's `input: discard` resolve to it. `RoutingDd` tests the sentinel before ever consulting the index, so this is belt-and-braces — but a shared index that holds a sentinel as a key is a trap for the next consumer.

**Discriminating check the reviewer should run, because the Spec tab and the Graph tab disagreeing is this wave's named failure mode:** does the Spec tab's index reproduce (a) multi-producer fan-in, (b) a queue publishing under its own id, (c) the row_union outbound rule? (a) and (b) yes — they are inside `buildConnectionProducers` and pinned by `graphTopology.test.ts`. (c) **no, deliberately**: `authoritativeRowUnionOutboundSemantics` (`GraphView.tsx:1270-1296`) is an edge-DRAWING rule about which of several semantics to label an arrow with, and the Spec tab draws no arrows. It is stated here so a future reviewer does not read its absence as an oversight.

**Ruling — `routingPhrase` returning null falls back to `routingValue`'s FOUR rules, not to `displayValue`.** `PipelineSpecView.tsx:51-69` is not a bare stringifier — `displayValue` (`:22-24`) is, and they are different functions. `routingValue` carries four rules that must all survive, enumerated here because "null means render as before" is not a specification:

1. **`:52` — `if (value === "discard") return "dropped (recorded in the audit trail)"`. Checked FIRST, before any other branch.** Pinned by `PipelineSpecView.test.tsx:157, :223, :278, :333` (`on_write_failure`) and `:541` (`on_validation_failure`) — five sites, two of them outside every range this task edits. In the rewrite this becomes the first thing `RoutingDd` tests, against `DISCARD_CONNECTION`.
2. **`:53` — `Array.isArray(value)` → `value.map(String).join(", ")`.** Covers `fork_to` and a bare-`string[]` `branches`.
3. **`:61-67` — a `routes` or `branches` OBJECT renders as `alias → target; alias → target`, never as a JSON string in a plain `<dd>`.** This rule exists because of `elspeth-b9ebdf9011`'s live-check fix: a coalesce's `branches` map rendered as a raw `{"branch_a":"target_a",…}` string. The existing `not.toHaveTextContent('{"')` pin stays and is the guard on it.
4. **`:63-65` — a `routes` map whose every target is `"fork"` renders "every row continues to all branches"**, ahead of rule 3.

Rules 1, 3 and 4 are *absorbed* into `specRouting` (rule 3's targets now resolve through the index to component phrases, which is a deliberate improvement layered on top of it, not a replacement for it); rule 2 stays where it is. **Nothing may reach `displayValue` that reached one of these four before.** Cost if wrong: a live-check fix from `elspeth-b9ebdf9011` regresses to raw JSON in a `<dd>`, and the pin that caught it last time is in a fixture range this task was not told it was editing.

**Ruling — `componentPhrase` consults a DESCRIPTION for sources and outputs too, and the `<h4>` does NOT use `componentPhrase`.** Two defects met here and they pull in opposite directions, so both are settled explicitly:

*(a) The ladder must include descriptions for all three component kinds.* The Global Constraints state the prose ladder as *"description → acronym-aware title case of the id → plugin gloss"*, and `lib/validationHumaniser.ts:162+` `makePhraseFor` — the shared authority for the summary headline, the wire-stage blockers, the audit panel and the chat injection — overlays `descriptionLabel` for sources, nodes and outputs alike (`:181-192`). But `stepLabelForNodeId` consults a description for **nodes only** (`interpretationStepLabel.ts:138`, `state?.nodes.find(...)`); for a source (`:106-107`) or an output (`:108`) it goes plugin → `titleCaseLabel(id)`. So `componentPhrase = stepLabelForNodeId(...) ?? titleCaseLabel(id)` would make a source keyed `invoices_csv` described "Quarterly invoices" read **"Invoices Csv"** in the Spec tab and **"Quarterly invoices"** in the validation summary, the audit panel and the chat — the exact drift `makePhraseFor` exists to prevent. `componentPhrase` therefore adds the missing rung itself:

```ts
export function componentPhrase(state: CompositionState, id: string): string {
  const described = descriptionLabel(
    state.sources[id]?.description ?? state.outputs.find((o) => o.name === id)?.description ?? null,
  );
  return described ?? stepLabelForNodeId(state, id) ?? titleCaseLabel(id);
}
```

It still does NOT call `humaniseStepLabel`: `componentPhrase` is called with ids the index already resolved (present by construction) and must never say "Removed" about a connection name. `makePhraseFor`'s fuzzy-match and `UNKNOWN_COMPONENT_PHRASE` rungs are also deliberately unwanted here for the same reason. **`descriptionLabel` is ALREADY exported** — `components/chat/interpretationStepLabel.ts:69`, and `lib/validationHumaniser.ts:35` already imports it, which is what makes this reuse rather than a fourth ladder. Import it; do not re-implement the truncation rule, which would be precisely the archetype this wave is closing. `SourceSpec.description` (`types/index.ts:141`) and `OutputSpec.description` (`:210`) both exist as `string | null` optionals, so the `?? null` coalescing above is required and correct.

*(b) The `<h4>` uses `titleCaseLabel(row.id)`, NOT `componentPhrase`, because the description already has its own slot directly beneath it.* `PipelineSpecView.tsx:96-100` renders `row.description` as `<p className="pipeline-spec-step-description">` immediately below the heading. A described plugin-less structural node — a coalesce, row_union, queue, gate, or collector-without-plugin, which is the NORM for composer-authored graphs — would therefore render *"Merge the two assessment branches"* as its `<h4>` and *"Merge the two assessment branches."* as the paragraph one line down: the same sentence twice, with the card losing its name entirely (the id surviving only in `title`). So:

```tsx
<h4 title={row.id}>{titleCaseLabel(row.id)}</h4>
```

The routing `<dd>`s keep `componentPhrase`, where there is no sibling paragraph and the description is the most informative thing available. **Add a test with a described coalesce pinning heading ≠ paragraph** (Step 3 item 7) — neither the existing coverage nor a pre-fix draft's new tests caught this: `PipelineSpecView.test.tsx:456-532` uses only plugin-bearing nodes (`csv_file`, `llm`, `passthrough`, `json`), and the draft's new test #1 used a `row_union` with no description at all.

Cost if wrong: (a) the Spec tab names a described source differently from every other prose surface; (b) every structural card reads its own description twice and shows no name.

**Ruling — `scope_opener` is the one routing field where "Removed" is the honest word, and the only consumer of `isComponentPresent` here.** Every other routing value names a CONNECTION, and a connection with no component on the far end is *dangling*, not removed — "Removed" would assert a deletion that may never have happened. `scope_opener` is different: it names a COMPONENT directly (the node that opens the expand group this collector closes), so `isComponentPresent` applies exactly as it does on an acknowledgement card, and a deleted opener should read "Removed" rather than being title-cased into a component that no longer exists. This is why Task 1's declared `isComponentPresent` interface is real and used, and why `componentPhrase` keeps its own title-case ladder rather than calling `humaniseStepLabel` — `componentPhrase` is called with ids the index already resolved (present by construction) and must never say "Removed" about a connection name. Cost if wrong: a collector whose opener was deleted reads "Explode Pages" for a node that is gone, instead of "Removed"; the raw id is in `title` either way.

- [ ] **Step 1: Write the failing pure tests**

`specRouting.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { makeComposition } from "@/test/composerFixtures";
import {
  buildConnectionIndex,
  componentPhrase,
  routingPhrase,
} from "./specRouting";

const state = makeComposition(9, {
  sources: {
    source: { plugin: "csv", options: {}, on_success: "raw_rows", on_validation_failure: "discard" },
  },
  nodes: [
    {
      id: "extract_invoice",
      node_type: "transform",
      plugin: "llm",
      input: "raw_rows",
      on_success: "invest_cs1_done",
      on_error: null,
      options: {},
    },
    {
      id: "merge_invest",
      node_type: "coalesce",
      plugin: null,
      input: "invest_cs1_done",
      on_success: "tidy_output",
      on_error: null,
      branches: { branch_invest_cs1: "invest_cs1_done", branch_invest_cs2: "invest_cs2_done" },
      policy: "require_all",
      merge: "union",
      options: {},
    },
  ],
  outputs: [{ name: "tidy_output", plugin: "csv", on_write_failure: "discard", options: {} }],
});
const index = buildConnectionIndex(state);

describe("buildConnectionIndex", () => {
  it("maps a connection to the components on each end", () => {
    expect(index.consumers.get("raw_rows")).toEqual(["extract_invoice"]);
    expect(index.producers.get("raw_rows")).toEqual(["source"]);
    expect(index.consumers.get("tidy_output")).toEqual(["tidy_output"]);
    expect(index.producers.get("invest_cs1_done")).toEqual(["extract_invoice"]);
  });

  it("registers a fan-in node against its BRANCH connections, never its placeholder input", () => {
    // core/config.py: branches is a "Branch identity -> input connection
    // mapping"; connection_consumers.py:31-40 skips node.input for
    // coalesce/row_union because that scalar is only the backend-compatible
    // first-branch placeholder. Both branches are consumed; neither is
    // produced by the coalesce (its on_success is tidy_output).
    expect(index.consumers.get("invest_cs1_done")).toEqual(["merge_invest"]);
    expect(index.consumers.get("invest_cs2_done")).toEqual(["merge_invest"]);
    expect(index.producers.get("invest_cs2_done")).toBeUndefined();
    expect(index.producers.get("tidy_output")).toEqual(["merge_invest"]);
  });

  it("credits a queue/coalesce/aggregation with its implicit self-published connection", () => {
    // publishedSuccessConnection, not node.on_success: a fan-in node with no
    // on_success publishes under its own id (session 3f02c8fa).
    const implicit = makeComposition(12, {
      sources: { source: { plugin: "csv", options: {}, on_success: "rows" } },
      nodes: [
        { id: "hold", node_type: "queue", plugin: null, input: "rows", on_success: null, on_error: null, options: {} },
      ],
      outputs: [{ name: "hold", plugin: "csv", on_write_failure: "discard", options: {} }],
    });
    expect(buildConnectionIndex(implicit).producers.get("hold")).toEqual(["hold"]);
  });
});

describe("componentPhrase", () => {
  it("uses the shared step label, title-casing an unlabelable id", () => {
    expect(componentPhrase(state, "extract_invoice")).toBe("Extract Invoice");
    expect(componentPhrase(state, "merge_invest")).toBe("Merge Invest");
    expect(componentPhrase(state, "tidy_output")).toBe("Tidy Output");
  });
});

describe("routingPhrase", () => {
  it("names the consumer for a downstream connection and keeps the raw name", () => {
    expect(routingPhrase(state, index, "on_success", "raw_rows")).toEqual({
      text: "Extract Invoice",
      raw: "raw_rows",
    });
  });

  it("names the producer for an input connection", () => {
    expect(routingPhrase(state, index, "input", "invest_cs1_done")).toEqual({
      text: "Extract Invoice",
      raw: "invest_cs1_done",
    });
  });

  it("title-cases a dangling connection instead of leaking it", () => {
    expect(routingPhrase(state, index, "on_success", "nowhere_yet")).toEqual({
      text: "Nowhere Yet",
      raw: "nowhere_yet",
    });
  });

  it("resolves a branch map UPSTREAM — the producer feeding the branch, never the fan-in node itself", () => {
    // The regression this test exists for: resolving branches downstream
    // yields "Branch Invest Cs1 -> Merge Invest", a node pointing at itself,
    // because a fan-in node's own `input` is one of its branch connections.
    expect(
      routingPhrase(state, index, "branches", {
        branch_invest_cs1: "invest_cs1_done",
        branch_invest_cs2: "invest_cs2_done",
      }),
    ).toEqual({
      text: "Branch Invest Cs1 → Extract Invoice; Branch Invest Cs2 → Invest Cs2 Done",
      raw: "branch_invest_cs1 → invest_cs1_done; branch_invest_cs2 → invest_cs2_done",
    });
  });

  it("expands a list-form branches to the identity mapping (a coalesce arrives holding a list)", () => {
    expect(routingPhrase(state, index, "branches", ["invest_cs1_done"])).toEqual({
      text: "Invest Cs1 Done → Extract Invoice",
      raw: "invest_cs1_done → invest_cs1_done",
    });
  });

  it("keeps the all-fork routes sentence and phrases other route targets", () => {
    expect(routingPhrase(state, index, "routes", { a: "fork", b: "fork" })?.text).toBe(
      "every row continues to all branches",
    );
    expect(routingPhrase(state, index, "routes", { rejected: "tidy_output" })).toEqual({
      text: "Rejected → Tidy Output",
      raw: "rejected → tidy_output",
    });
  });

  it("phrases the closed policy enums and falls back to title case for an unknown value", () => {
    expect(routingPhrase(state, index, "policy", "require_all")).toEqual({
      text: "wait for every branch",
      raw: "require_all",
    });
    expect(routingPhrase(state, index, "merge", "union")).toEqual({
      text: "combine every branch's fields",
      raw: "union",
    });
    expect(routingPhrase(state, index, "scope_policy", "require_all")).toEqual({
      text: "wait for every row in the group",
      raw: "require_all",
    });
    expect(routingPhrase(state, index, "output_mode", "passthrough")).toEqual({
      text: "pass rows through unchanged",
      raw: "passthrough",
    });
    expect(routingPhrase(state, index, "output_mode", "default")).toEqual({
      text: "use the plugin's own behaviour",
      raw: "default",
    });
    expect(routingPhrase(state, index, "policy", "someday_maybe")).toEqual({
      text: "Someday Maybe",
      raw: "someday_maybe",
    });
  });

  it("names the opener node for scope_opener, says Removed for a deleted one, and passes discard / numbers through as null", () => {
    expect(routingPhrase(state, index, "scope_opener", "extract_invoice")).toEqual({
      text: "Extract Invoice",
      raw: "extract_invoice",
    });
    // scope_opener names a COMPONENT, not a connection, so absence is a
    // deletion and reads as one (Task 1's isComponentPresent).
    expect(routingPhrase(state, index, "scope_opener", "deleted_opener")).toEqual({
      text: "Removed",
      raw: "deleted_opener",
    });
    expect(routingPhrase(state, index, "on_write_failure", "discard")).toBeNull();
    expect(routingPhrase(state, index, "timeout_seconds", 300)).toBeNull();
  });
});

describe("buildConnectionIndex — the branches the ruling states and nothing tested", () => {
  // Every case below is a rule this module DOCUMENTS. A documented rule with
  // no test is the shape this whole wave exists to remove.

  it("keeps ordinary `input` inference for a fan-in node with NO branches", () => {
    // The `entries.length > 0` guard in nodeInputs, matching GraphView's
    // aliasMappedFanInIds guard (GraphView.tsx:1363-1369). Silent failure
    // mode: a mid-edit coalesce drops out of the consumer index entirely, so
    // every routing field pointing at it title-cases instead of naming it —
    // indistinguishable from a dangling connection. All three empty shapes
    // reach the same arm and all three are representable on the wire.
    for (const branches of [null, [], {}] as const) {
      const state = makeComposition(1, {
        sources: { source: { plugin: "csv", options: {}, on_success: "raw_rows" } },
        nodes: [{ id: "merge", node_type: "coalesce", plugin: null, input: "raw_rows",
                  on_success: "final_out", on_error: null, branches, options: {} }],
        outputs: [{ name: "final_out", plugin: "csv", options: {} }],
      });
      expect(buildConnectionIndex(state).consumers.get("raw_rows")).toContain("merge");
    }
  });

  it("registers BOTH consumers of one connection", () => {
    // connectionPhrase joins with ", "; a single-consumer-only index would
    // read the same in every existing fixture.
    const state = makeComposition(1, {
      sources: { source: { plugin: "csv", options: {}, on_success: "raw_rows" } },
      nodes: [
        { id: "score", node_type: "transform", plugin: "llm", input: "raw_rows", on_success: "a", on_error: null, options: {} },
        { id: "audit", node_type: "transform", plugin: "llm", input: "raw_rows", on_success: "b", on_error: null, options: {} },
      ],
      outputs: [],
    });
    expect(buildConnectionIndex(state).consumers.get("raw_rows")?.sort()).toEqual(["audit", "score"]);
  });

  it("registers a source's on_validation_failure and an output's on_write_failure as producers, but never `discard`", () => {
    // The two additions this task layers on top of buildConnectionProducers.
    // The discard guard keeps a sentinel out of the shared index.
    const state = makeComposition(1, {
      sources: { source: { plugin: "csv", options: {}, on_success: "raw_rows", on_validation_failure: "rejects" } },
      nodes: [{ id: "triage", node_type: "transform", plugin: "llm", input: "rejects", on_success: "final_out", on_error: null, options: {} }],
      outputs: [
        { name: "final_out", plugin: "csv", options: {}, on_write_failure: "write_errors" },
        { name: "write_errors", plugin: "csv", options: {}, on_write_failure: "discard" },
      ],
    });
    const { producers } = buildConnectionIndex(state);
    expect(producers.get("rejects")).toEqual(["source"]);
    expect(producers.get("write_errors")).toEqual(["final_out"]);
    expect(producers.has("discard")).toBe(false);
  });
});

describe("routingPhrase — the partly-fork routes map", () => {
  it("renders a mixed routes map alias-by-alias rather than as 'every row continues to all branches'", () => {
    // routingValue.:63-65's every()-guard is false here, so it falls through
    // to the alias-by-alias arm. The "fork" arm renders as the sentinel word
    // and NOT as a resolved component — pinned so the output is a decision
    // rather than an accident.
    const state = makeComposition(1, {
      sources: { source: { plugin: "csv", options: {}, on_success: "raw_rows" } },
      nodes: [
        { id: "split", node_type: "gate", plugin: null, input: "raw_rows", on_success: null, on_error: null,
          routes: { kept: "tidy_output", every: "fork" }, fork_to: ["arm_a"], options: {} },
        { id: "tidy", node_type: "transform", plugin: "llm", input: "tidy_output", on_success: "final_out", on_error: null, options: {} },
      ],
      outputs: [{ name: "final_out", plugin: "csv", options: {} }],
    });
    const index = buildConnectionIndex(state);
    const phrase = routingPhrase(state, index, "routes", { kept: "tidy_output", every: "fork" });
    expect(phrase?.text).toBe("Kept → Tidy; Every → Fork");
    expect(phrase?.raw).toBe("kept → tidy_output; every → fork");
  });
});
```

**On that last expectation:** "Every → Fork" is what the alias-by-alias arm produces for a `"fork"` target, because `buildConnectionProducers` excludes the sentinel from the index and the unresolved value then title-cases. It reads slightly oddly. **Ruling — leave it, and pin it.** The alternative (special-casing a partial fork into prose) invents a phrase for a shape the backend permits but the composer has never authored, and an odd-but-honest rendering with the raw in `title` is better than an invented one. Cost if wrong: one awkward `<dd>` on a hand-written gate; the raw map is one hover away. If a real pipeline ever produces this, the pin is where to change it.

Run: `npx vitest run src/components/workspace/specRouting.test.ts` → FAIL (module missing).

- [ ] **Step 2: Implement `specRouting.ts`**

```ts
// ============================================================================
// specRouting — reader-register phrases for the Spec tab's routing <dd>s
// (elspeth-93f5621f18, Wave 3; carries the elspeth-b9ebdf9011 branches-as-
// prose fix from PipelineSpecView.routingValue). A routing value is one of
// three things:
//   * a CONNECTION name (`on_success: raw_rows`) — the reader wants the
//     component on the other end, resolved from the composition itself;
//   * a closed POLICY enum (`policy: require_all`) — a fixed phrase;
//   * a passthrough (`discard`, numbers) — left to the caller.
// Every phrase carries `raw` so the renderer can put the wire form in
// `title`. Pure: reads a CompositionState, never mutates.
//
// The topology rules — what a node publishes, and what a fan-in node reads —
// are NOT decided here. They live in lib/graphTopology.ts, lifted out
// of GraphView so the Spec tab and the Graph tab cannot disagree about the
// same pipeline. In particular `branches` resolves UPSTREAM: a fan-in node's
// branches are its inputs (core/config.py CoalesceSettings.branches), and its own
// `input` is only a first-branch placeholder, so resolving them downstream
// makes the node name itself.
// ============================================================================

import { titleCaseLabel } from "@/components/catalog/pluginDisplayName";
import {
  isComponentPresent,
  stepLabelForNodeId,
} from "@/components/chat/interpretationStepLabel";
import {
  branchEntries,
  publishedSuccessConnection,
  COALESCE_MERGES,
  COALESCE_POLICIES,
  DISCARD_CONNECTION,
  FAN_IN_NODE_TYPES,
  type CoalesceMerge,
  type CoalescePolicy,
} from "@/lib/graphTopology";
import { sortedSourceEntries } from "@/utils/compositionState";
import type { CompositionState, NodeSpec } from "@/types/index";

export interface RoutingPhrase {
  text: string;
  raw: string;
}

export interface ConnectionIndex {
  /** connection → ids of the components that READ it (node.input, a fan-in
   *  node's branch connections, output.name) */
  consumers: Map<string, string[]>;
  /** connection → ids of the components that WRITE it (published success,
   *  on_error, routes, fork_to) */
  producers: Map<string, string[]>;
}

/**
 * Fields whose value the node WRITES and whose shape is a scalar or an array
 * of connection names; resolve through `consumers`.
 *
 * This is a SHAPE partition, not a direction one, and it is deliberately NOT
 * the same set as the backend's routing-field Literal
 * (web/composer/guided/planning.py:123 and :569, which enumerate
 * on_success | on_error | on_validation_failure | on_write_failure | routes |
 * fork_to). `routes` is genuinely outbound and is missing here only because
 * it is MAP-shaped and handled below alongside `branches`; `branches` is
 * absent for the same shape reason, and is inbound as well. Do not "fix" this
 * set to match planning.py's — that would send a map-shaped value down the
 * scalar path — and do not read its omissions as statements about direction.
 * The direction rule is the table in this task's ruling; its authority is
 * connection_consumers.py:31-40, not this set.
 */
const DOWNSTREAM_FIELDS: ReadonlySet<string> = new Set([
  "on_success",
  "on_error",
  "on_validation_failure",
  "on_write_failure",
  "fork_to",
]);

// The three CLOSED maps below are keyed by a UNION type, not by `string`, so
// adding a member to the backend Literal without phrasing it here is a BUILD
// failure rather than a silent title-cased leak at runtime. Membership comes
// from lib/graphTopology (policy, merge) and from types/index.ts:183
// (output_mode); this file states PHRASES and never membership.
export const POLICY_PHRASES: Record<CoalescePolicy, string> = {
  require_all: "wait for every branch",
  quorum: "wait for a quorum of branches",
  best_effort: "use whichever branches arrive",
  first: "take the first branch to arrive",
};

export const MERGE_PHRASES: Record<CoalesceMerge, string> = {
  union: "combine every branch's fields",
  nested: "keep each branch's fields under its own name",
  select: "keep the selected branch's fields",
};

export const OUTPUT_MODE_PHRASES: Record<NonNullable<NodeSpec["output_mode"]>, string> = {
  // "default" is a sentence like its siblings, not the bare enum word: a <dd>
  // reading "default" tells the reader nothing they could not see in the YAML.
  default: "use the plugin's own behaviour",
  passthrough: "pass rows through unchanged",
  transform: "emit transformed rows",
};

// scope_policy is the ONE open map here: it has no backend Literal and no
// frontend member set to close against (types/index.ts:191 types it
// `string | null`, and a collector's arrival policy is not a coalesce's).
// Left as a Map over `string` deliberately — see this task's ruling.
export const SCOPE_POLICY_PHRASES: ReadonlyMap<string, string> = new Map([
  ["require_all", "wait for every row in the group"],
  ["best_effort", "close the group with whichever rows arrive"],
]);

function closedPhrase(map: Record<string, string>, value: string): string | undefined {
  return Object.prototype.hasOwnProperty.call(map, value) ? map[value] : undefined;
}

const ENUM_FIELDS: ReadonlyMap<string, (value: string) => string | undefined> = new Map([
  ["policy", (value: string) => closedPhrase(POLICY_PHRASES, value)],
  ["merge", (value: string) => closedPhrase(MERGE_PHRASES, value)],
  ["scope_policy", (value: string) => SCOPE_POLICY_PHRASES.get(value)],
  ["output_mode", (value: string) => closedPhrase(OUTPUT_MODE_PHRASES, value)],
]);

function push(map: Map<string, string[]>, key: string, id: string): void {
  const existing = map.get(key);
  if (existing === undefined) map.set(key, [id]);
  else if (!existing.includes(id)) existing.push(id);
}

/** Connections this node READS. For a fan-in kind that is its branch
 *  connections and NOT its `input` — the canonical consumer projection
 *  (web/composer/guided/connection_consumers.py:31-40) skips `input` for
 *  coalesce/row_union because it is only the first-branch placeholder. A
 *  fan-in node with no branches at all keeps ordinary `input` inference,
 *  matching GraphView's `aliasMappedFanInIds` guard. */
function nodeInputs(node: NodeSpec): string[] {
  if (FAN_IN_NODE_TYPES.has(node.node_type)) {
    const entries = branchEntries(node.branches);
    if (entries.length > 0) return entries.map(([, connection]) => connection);
  }
  return node.input ? [node.input] : [];
}

export function buildConnectionIndex(state: CompositionState): ConnectionIndex {
  // PRODUCERS: the shared rule, from lib/graphTopology. Do NOT re-derive the
  // four registration rules here — GraphView.tsx owned them privately once and
  // that is the defect Task 3 closed (see the index ruling). `nodeTargets` is
  // gone: it was this module's private restatement of the same rule.
  const producers = buildConnectionProducers(state);
  const consumers = new Map<string, string[]>();

  // The two registrations the Graph tab does not make, layered on top so its
  // behaviour cannot change. Verified: `on_validation_failure` appears in
  // GraphView.tsx only at :628 (a config-panel field) and `on_write_failure`
  // not at all. Both are needed here because the Spec tab prints every routing
  // field as prose, including the failure lanes; omitting either would be
  // asymmetric for no reason a reader could infer. `discard` is not a
  // connection (_producer_resolver.py:208), so it never becomes a key.
  for (const [name, source] of sortedSourceEntries(state)) {
    if (source.on_validation_failure && source.on_validation_failure !== DISCARD_CONNECTION) {
      push(producers, source.on_validation_failure, name);
    }
  }
  for (const output of state.outputs) {
    if (output.on_write_failure && output.on_write_failure !== DISCARD_CONNECTION) {
      push(producers, output.on_write_failure, output.name);
    }
  }

  // CONSUMERS: genuinely new — GraphView has no consumer map, it resolves the
  // consumer direction by looking node.input UP against the producer registry.
  for (const node of state.nodes) {
    for (const connection of nodeInputs(node)) push(consumers, connection, node.id);
  }
  for (const output of state.outputs) {
    if (output.input) push(consumers, output.input, output.name);
    push(consumers, output.name, output.name);   // an output IS its own connection
  }
  return { consumers, producers };
}

/** The reader-register name for a source, node or output.
 *
 *  The ladder is description -> acronym-aware title case of the id, matching
 *  lib/validationHumaniser.ts's makePhraseFor (:181-192), which overlays a
 *  description for all THREE component kinds. `stepLabelForNodeId` alone
 *  consults a description for nodes only (interpretationStepLabel.ts:138), so
 *  a described source would read "Invoices Csv" here and "Quarterly invoices"
 *  in the validation summary, the audit panel and the chat.
 *
 *  Never "Removed": this is called with ids the index already resolved, and
 *  with connection names, where absence means dangling rather than deleted.
 *  Not for the <h4> either — the description has its own <p> directly beneath
 *  the heading (PipelineSpecView.tsx:96-100), so a described structural node
 *  would print the same sentence twice. See the description-rung ruling. */
export function componentPhrase(state: CompositionState, id: string): string {
  const described = descriptionLabel(
    state.sources[id]?.description
      ?? state.outputs.find((output) => output.name === id)?.description
      ?? null,
  );
  return described ?? stepLabelForNodeId(state, id) ?? titleCaseLabel(id);
}

function connectionPhrase(
  state: CompositionState,
  index: ConnectionIndex,
  connection: string,
  direction: "downstream" | "upstream",
): string {
  const ids = (direction === "downstream" ? index.consumers : index.producers).get(connection);
  if (ids === undefined || ids.length === 0) return titleCaseLabel(connection);
  return ids.map((id) => componentPhrase(state, id)).join(", ");
}

function mapPhrase(
  state: CompositionState,
  index: ConnectionIndex,
  entries: [string, string][],
  direction: "downstream" | "upstream",
): RoutingPhrase {
  return {
    text: entries
      .map(([alias, target]) => `${titleCaseLabel(alias)} → ${connectionPhrase(state, index, target, direction)}`)
      .join("; "),
    raw: entries.map(([alias, target]) => `${alias} → ${target}`).join("; "),
  };
}

/**
 * The reader-register phrase for one routing field, or null when the value
 * is not a connection or enum (`discard`, numbers, nulls) and the caller's
 * existing rendering applies.
 */
export function routingPhrase(
  state: CompositionState,
  index: ConnectionIndex,
  field: string,
  value: unknown,
): RoutingPhrase | null {
  if (value === DISCARD_CONNECTION || value === null || value === undefined) return null;
  const enumLookup = ENUM_FIELDS.get(field);
  if (enumLookup !== undefined && typeof value === "string") {
    // The maps are closed at COMPILE time; this runtime fallback exists only
    // because types/index.ts:180-181,191 still type policy/merge/scope_policy
    // as `string | null`, so a wire value outside the union is representable.
    return { text: enumLookup(value) ?? titleCaseLabel(value), raw: value };
  }
  if (field === "scope_opener" && typeof value === "string") {
    // The one routing field naming a COMPONENT rather than a connection, so
    // the one where absence honestly means "removed" (see the task ruling).
    return {
      text: isComponentPresent(state, value) ? componentPhrase(state, value) : "Removed",
      raw: value,
    };
  }
  if (field === "input" && typeof value === "string") {
    return { text: connectionPhrase(state, index, value, "upstream"), raw: value };
  }
  if (DOWNSTREAM_FIELDS.has(field)) {
    if (typeof value === "string") {
      return { text: connectionPhrase(state, index, value, "downstream"), raw: value };
    }
    if (Array.isArray(value)) {
      const names = value.map(String);
      return {
        text: names.map((name) => connectionPhrase(state, index, name, "downstream")).join(", "),
        raw: names.join(", "),
      };
    }
  }
  if (field === "routes" || field === "branches") {
    // `routes` is fan-OUT (the gate writes these), `branches` is fan-IN (the
    // coalesce reads these). Resolving branches downstream makes the node
    // name itself, because its own `input` is one of its branch connections.
    const direction = field === "branches" ? "upstream" : "downstream";
    if (Array.isArray(value)) {
      // A list-form `branches` IS the identity mapping (config.py
      // normalize_branches); branchEntries owns that rule. A list-form
      // `routes` is not a thing, but the array arm is kept for both so an
      // unexpected shape still renders as prose rather than JSON.
      const entries: [string, string][] =
        field === "branches"
          ? branchEntries(value.map(String))
          : value.map((name): [string, string] => [String(name), String(name)]);
      return mapPhrase(state, index, entries, direction);
    }
    if (typeof value === "object") {
      const entries = Object.entries(value as Record<string, unknown>).map(
        ([alias, target]): [string, string] => [alias, String(target)],
      );
      if (field === "routes" && entries.every(([, target]) => target === "fork")) {
        return { text: "every row continues to all branches", raw: entries.map(([a]) => `${a} → fork`).join("; ") };
      }
      return mapPhrase(state, index, entries, direction);
    }
  }
  return null;
}
```

Run: `npx vitest run src/components/workspace/specRouting.test.ts` → PASS. (If `makeComposition`'s source shape rejects `on_validation_failure`, check `src/test/composerFixtures.ts` and adjust the fixture, not the module.)

- [ ] **Step 3: Write the failing Spec-tab tests**

**Task 2 must have landed before this step compiles.** `test/defaultDomPins.ts:37-40` today accepts only `{ allowSelectors?: readonly string[] }`, and its aria loop (`:49-55`) has no exemption mechanism at all beyond the ToolCallInfo trigger. The `allowAriaLabelSelectors` argument below is a **TypeScript error**, not a soft failure, until Task 2 Step 2 lands. If you picked this task up alone, stop and do Task 2 first.

In `PipelineSpecView.test.tsx`, update the existing pins and add the default-DOM pin (imports: add `expectNoIdentifiersInDefaultDom` from `@/test/defaultDomPins`):

1. `:192-249` ("projects a coalesce's fan-in config"): `toHaveTextContent("branch_a")` → `toHaveTextContent("Branch A → Pairing Done")`; `toHaveTextContent("hex_done")` → `toHaveTextContent("Branch B → Hex Done")`; `toHaveTextContent("require_all")` → `toHaveTextContent("wait for every branch")`; `toHaveTextContent("union")` → `toHaveTextContent("combine every branch's fields")`; add `expect(within(node).getByText("wait for every branch")).toHaveAttribute("title", "require_all");`. (Both aliases title-case rather than resolving: that fixture's only source publishes `colours_raw` and the coalesce publishes `final_out`, so nothing produces `pairing_done` or `hex_done` and the upstream lookup falls through to `titleCaseLabel` — which is the correct reading of a fixture whose upstream arms are not modelled.)
2. `:251-292` ("renders a coalesce's branch map as prose"): the `toHaveTextContent("branch_invest_cs1 → invest_cs1_done; …")` becomes `toHaveTextContent("Branch Invest Cs1 → Invest Cs1 Done; Branch Invest Cs2 → Invest Cs2 Done")` (again, no producer for either connection in that fixture) and add `expect(within(node).getByText(/^Branch Invest Cs1/)).toHaveAttribute("title", "branch_invest_cs1 → invest_cs1_done; branch_invest_cs2 → invest_cs2_done");`. The existing `expect(node).not.toHaveTextContent('{"')` assertion stays — it is the elspeth-b9ebdf9011 regression pin and is still exactly the right check.
3. `:294-355` (collector): `Scope` is a scope NAME — neither an enum nor a connection — so `routingPhrase` returns null and the caller would fall to `displayValue`, leaking `doc_pages`. Add `scope_name` to the renderer's title-case path (Step 4, `AUTHOR_NAME_FIELDS`) and pin `toHaveTextContent("Doc Pages")` with `title="doc_pages"`. `toHaveTextContent("require_all")` → `"wait for every row in the group"`; `toHaveTextContent("passthrough")` → `"pass rows through unchanged"`; `300` unchanged.

   **`toHaveTextContent("explode_pages")` needs a FIXTURE change, not just a re-pin, and this is a decision the plan makes rather than leaving to the implementer.** That test ("projects a collector's scope binding") builds `makeComposition(6, …)` whose only node is `gather_pages` (`.test.tsx:317-332`) with `scope_opener: "explode_pages"` (`:325`); sources are `{ source: … }` and outputs are `[{ name: "final_out", … }]`. **No component named `explode_pages` exists in that fixture at all.** `scope_opener` is the one field that runs through `isComponentPresent(state, value) ? componentPhrase(...) : "Removed"` (see the `scope_opener` ruling), so the `<dd>` would render **`Removed`** and a pin of `"Explode Pages"` fails. A pre-fix draft re-pinned it to `"Explode Pages"` and would have gone red on its own first run.

   **Do this:** add the missing opener to the `:294` fixture — an `explode_pages` node of `node_type: "expand"` (or whatever kind the collector's opener is in the corpus; a `transform` also satisfies `isComponentPresent`, which is pure membership) wired ahead of `gather_pages` — and keep the pin `toHaveTextContent("Explode Pages")` with `title="explode_pages"`. This preserves what the test is *for*: it is about projecting a collector's scope BINDING, and an unresolvable opener is not that.

   **Then add the complementary case as a new `it()`,** because today neither the unit test nor the component test exercises both arms of `scope_opener` against the same field:

   ```tsx
     it("says Removed for a collector whose scope opener was deleted (elspeth-93f5621f18)", () => {
       // The other arm of the scope_opener ruling: every OTHER routing value
       // names a connection, where a missing far end is dangling rather than
       // removed. scope_opener names a COMPONENT, so a deleted opener is the
       // one place "Removed" is the honest word.
       useSessionStore.setState({
         compositionState: makeComposition(14, {
           sources: { source: { plugin: "csv", options: {}, on_success: "raw_rows" } },
           nodes: [
             { id: "gather_pages", node_type: "collector", plugin: null, input: "raw_rows", on_success: "final_out", on_error: null, scope_name: "doc_pages", scope_opener: "explode_pages", scope_policy: "require_all", output_mode: "passthrough", timeout_seconds: 300, options: {} },
           ],
           outputs: [{ name: "final_out", plugin: "csv", options: {} }],
         }),
       });
       render(<PipelineSpecView />);
       const node = screen.getByRole("article", { name: "Node gather_pages" });
       const dd = within(node).getByText("Scope opened by").nextElementSibling;
       expect(dd).toHaveTextContent("Removed");
       expect(dd).toHaveAttribute("title", "explode_pages");
     });
   ```
4. `:129-190` ("shows only non-null authoritative routing fields"): unchanged assertions (labels only) — but the card's `<h4>` now reads "Rows" not "rows"; nothing there pins the heading text.
5. `:507` — no change needed. That test's `getByRole("heading", { level: 4 })` is used only as an argument to `compareDocumentPosition` (verified: the heading query is at `PipelineSpecView.test.tsx:507` and the document-order assertion at `:508-511` — it asserts order, not text), so the heading's new title-cased content does not reach an assertion.
6. New:

```tsx
  it("renders node ids and kinds in the reader register with the raw id in title (elspeth-93f5621f18 / elspeth-d74ab492dd)", () => {
    useSessionStore.setState({
      compositionState: makeComposition(10, {
        sources: { source: { plugin: "csv", options: {}, on_success: "raw_rows" } },
        nodes: [
          { id: "extract_invoice", node_type: "transform", plugin: "llm", input: "raw_rows", on_success: "results", on_error: null, options: {} },
          { id: "split_rows", node_type: "row_union", plugin: null, input: "results", on_success: "results", on_error: null, options: {} },
        ],
        outputs: [{ name: "results", plugin: "csv", on_write_failure: "discard", options: {} }],
      }),
    });
    render(<PipelineSpecView />);
    const node = screen.getByRole("article", { name: "Node extract_invoice" });
    expect(within(node).getByRole("heading", { level: 4 })).toHaveTextContent("Extract Invoice");
    expect(within(node).getByRole("heading", { level: 4 })).toHaveAttribute("title", "extract_invoice");
    expect(within(node).getByText("Reads from").nextElementSibling).toHaveTextContent("Source");
    expect(within(node).getByText("Then").nextElementSibling).toHaveTextContent("Results");
    expect(within(node).getByText("Then").nextElementSibling).toHaveAttribute("title", "results");
    const union = screen.getByRole("article", { name: "Node split_rows" });
    expect(within(union).getByText("Kind").nextElementSibling).toHaveTextContent("Row Union");
  });

  it("names the component feeding each branch of a wired coalesce (elspeth-93f5621f18)", () => {
    // The direction pin. A fan-in node's own `input` is one of its own branch
    // connections, so resolving `branches` through consumers rather than
    // producers renders "Branch Invest Cs1 → Merge Invest" — the node naming
    // itself. This fixture WIRES the upstream arm so the two directions give
    // visibly different answers and the wrong one cannot pass.
    useSessionStore.setState({
      compositionState: makeComposition(13, {
        sources: { source: { plugin: "csv", options: {}, on_success: "raw_rows" } },
        nodes: [
          { id: "extract_invoice", node_type: "transform", plugin: "llm", input: "raw_rows", on_success: "invest_cs1_done", on_error: null, options: {} },
          { id: "merge_invest", node_type: "coalesce", plugin: null, input: "invest_cs1_done", on_success: "tidy_output", on_error: null, branches: { branch_invest_cs1: "invest_cs1_done", branch_invest_cs2: "invest_cs2_done" }, policy: "require_all", merge: "union", options: {} },
        ],
        outputs: [{ name: "tidy_output", plugin: "csv", on_write_failure: "discard", options: {} }],
      }),
    });
    render(<PipelineSpecView />);
    const node = screen.getByRole("article", { name: "Node merge_invest" });
    expect(within(node).getByText("Merges branches").nextElementSibling).toHaveTextContent(
      "Branch Invest Cs1 → Extract Invoice; Branch Invest Cs2 → Invest Cs2 Done",
    );
    expect(node).not.toHaveTextContent("Branch Invest Cs1 → Merge Invest");
  });

  it("default DOM of the Spec tab passes the shared identifier pin (card names exempted by design)", () => {
    useSessionStore.setState({
      compositionState: makeComposition(11, {
        sources: { source: { plugin: "csv", options: {}, on_success: "raw_rows", on_validation_failure: "discard" } },
        nodes: [
          { id: "merge_invest", node_type: "coalesce", plugin: null, input: "invest_cs1_done", on_success: "tidy_output", on_error: null, branches: { branch_invest_cs1: "invest_cs1_done" }, policy: "require_all", merge: "union", options: {} },
          { id: "collect_pages", node_type: "collector", plugin: null, input: "tidy_output", on_success: "tidy_output", on_error: null, scope_name: "doc_pages", scope_opener: "merge_invest", scope_policy: "require_all", output_mode: "passthrough", timeout_seconds: 300, options: {} },
        ],
        outputs: [{ name: "tidy_output", plugin: "csv", on_write_failure: "discard", options: {} }],
      }),
    });
    const { container } = render(<PipelineSpecView />);
    expectNoIdentifiersInDefaultDom(container, {
      // SELF-only: both labels are ON these elements, so a control added
      // inside a spec card later is still scanned. See the ruling below.
      allowAriaLabelSelfSelectors: ["article.pipeline-spec-card", "div.option-rows"],
    });
  });
```

**Ruling — this uses `allowAriaLabelSelfSelectors` (the `matches()` option), NOT `allowAriaLabelSelectors`.** Two pre-fix drafts got this wrong in sequence. The first passed `[".pipeline-spec-card"]` to the subtree option; the second changed it to `["article.pipeline-spec-card"]` and claimed that made it "two exact elements". **Neither narrows anything**, because `el.closest(selector)` walks up ancestors (and checks the element itself first), so for every aria-labelled descendant of a spec card `closest` returns the card either way — the tag qualifier changes which elements MATCH the selector, not which elements the exemption REACHES. `PipelineSpecView.tsx:90` opens the `<article>` that wraps EVERY Spec-tab card, so **the subtree form turns the aria half of the pin off for the whole Spec tab, permanently, on the surface this task's acceptance rests on** — exactly what Task 2's ruling forbids one level lower (*"NOT the whole `.validation-banner-error-item` row"*). The self-only matcher exempts these two elements and scans everything under them, which is what "two exact elements" was always supposed to mean.

The two elements are known and enumerable, so enumerate them:

- **`article.pipeline-spec-card`** — the `<article>` opens at `PipelineSpecView.tsx:90` (`key` `:91`, `className` `:92`, `aria-label={`${singular} ${row.id}`}` `:93`). Kept raw because 15+ existing `getByRole("article", { name })` pins depend on it, and `<article>` maps to `role=article`, which supports naming from author — so an AT user still hears "Node extract_invoice" while a sighted user reads "Extract Invoice". That is the *point* of the exemption, not a workaround. (2.5.3 Label in Name does not apply: an `<article>` is not a user interface component.)
- **`div.option-rows`** — the `OptionRows` region. `components/inspector/OptionRows.tsx:194` is `<div className="option-rows" role="region" aria-label={ariaLabel}>`, and `PipelineSpecView.tsx:122-125` passes `ariaLabel={`${singular} ${row.id} settings`}` (the `<OptionRows` tag opens at `:122`, the prop is at `:124`; `:121` is the `</dl>` above it). (Note the file path: `components/inspector/OptionRows.tsx`, **not** `components/workspace/`. The Spec tab imports the inspector's shared renderer; several citations of this file elsewhere in the plan give the bare basename, and `src/styles/` and `components/workspace/` are both plausible-looking wrong homes.) **Expected to pass, with the enumeration recorded so a failure is diagnosable rather than mysterious — because switching from `closest()` to `matches()` removes a safety margin this fixture used to have.** Under `closest()` any aria-labelled descendant was silently covered; under `matches()` it is scanned, so the fixture must contain no third one. Swept: **`[aria-label]` inside a Spec-tab card is exactly these two elements.** `PipelineSpecView.tsx` carries only three aria attributes in the whole file — `role="region"` + `aria-labelledby={headingId}` at `:77-78` (the SECTION wrapper, outside the card; and `aria-labelledby` is not `aria-label`, so the helper's `querySelectorAll("[aria-label]")` does not select it at all) and the card's own `aria-label` at `:93`. `OptionRows.tsx` carries exactly one, the region at `:194`; its descendants — the `<details className="option-rows-advanced">` at `:201`, its `<summary>`, and the option `<dl>` — carry none. `PipelineGloss.tsx` carries no `aria-label` and no `role`. The shared `Button` sets no default `aria-label`. **If this pin goes red, a new aria-labelled element is the thing to look at — not the exemption**, and the fix is a third self-only selector with its own justification, never widening either of these two.

Under `matches()` each exempts only itself, so a control added inside a spec card in future is scanned rather than silently exempted — which is what the gate is for, and what Task 2 Step 1's fourth case pins.

**One more trap for whoever writes the next Spec-tab fixture, since it costs one sentence now and a confusing red run later:** `PipelineSpecView.tsx:237` renders `<PipelineGloss>`, whose text is scanned normally (the aria exemption does not touch the text scan). For THIS fixture the gloss renders only fixed phrases from `pipelineGloss.ts:55-113` and is clean. But `pipelineGloss.ts:83` emits `` `filter the rows (when ${condition})` `` — an authored predicate, verbatim. A fixture carrying a gate with a snake_case condition will fail this pin **on the gloss, not on the card**.


7. New — **the heading/description duplication pin** the description-rung ruling requires. Nothing in the existing file covers it (`:456-532` uses only plugin-bearing nodes) and a described structural node is the common shape, not an edge case:

```tsx
  it("does not repeat a structural node's description as its heading (elspeth-93f5621f18)", () => {
    // componentPhrase resolves a plugin-less node to descriptionLabel(node.description),
    // and PipelineSpecView.tsx:96-100 already renders that description as the
    // paragraph directly under the heading. Using componentPhrase for the <h4>
    // therefore prints the same sentence twice and leaves the card unnamed.
    useSessionStore.setState({
      compositionState: makeComposition(15, {
        sources: { source: { plugin: "csv", options: {}, on_success: "raw_rows" } },
        nodes: [
          { id: "merge_assessments", node_type: "coalesce", plugin: null, input: "raw_rows",
            on_success: "final_out", on_error: null,
            description: "Merge the two assessment branches",
            branches: { branch_a: "raw_rows", branch_b: "second_pass" },
            policy: "require_all", merge: "union", options: {} },
        ],
        outputs: [{ name: "final_out", plugin: "csv", options: {} }],
      }),
    });
    render(<PipelineSpecView />);
    const node = screen.getByRole("article", { name: "Node merge_assessments" });
    const heading = within(node).getByRole("heading", { level: 4 });
    expect(heading).toHaveTextContent("Merge Assessments");
    expect(heading).toHaveAttribute("title", "merge_assessments");
    // The description keeps its own slot and is NOT the heading.
    expect(within(node).getByText("Merge the two assessment branches")).not.toBe(heading);
  });
```

Run: `npx vitest run src/components/workspace/PipelineSpecView.test.tsx` → FAIL on the new/updated pins.

- [ ] **Step 4: Implement in `PipelineSpecView.tsx`**

1. Imports: add `import { titleCaseLabel } from "@/components/catalog/pluginDisplayName";`, `import { buildConnectionIndex, componentPhrase, routingPhrase, type ConnectionIndex } from "./specRouting";`.
2. `SpecRow` (`:7-15`): add `label: string;` after `id`. Each builder sets it: `label: componentPhrase(state, id)` (sources: `id` is the source key; nodes: `node.id`; outputs: `output.name`). **`label` is for the routing `<dd>`s and any future prose slot — it is NOT what the `<h4>` renders.** See item 6 and the description-rung ruling.
3. `SpecSectionProps` (`:17-20`): add `state: CompositionState; index: ConnectionIndex;`. `PipelineSpecView` (`:232-241`) computes `const index = buildConnectionIndex(compositionState);` once and passes `state={compositionState} index={index}` to the three sections.
4. `routingLabel` (`:47-49`) — close the snake_case fallback (the named Wave 2 deferral homed in `elspeth-93f5621f18`):

```tsx
function routingLabel(field: string): string {
  // A field absent from the map is still an author-visible <dt>; title-case
  // it rather than printing bare snake_case. ROUTING_LABELS is believed
  // exhaustive over the fields *Rows() projects today, so this is a guard
  // against a future field being added to a builder and not to the map —
  // exactly the drift the Wave 1 live check found on the <dd> side.
  return ROUTING_LABELS[field] ?? titleCaseLabel(field);
}
```

This closes the last of the five Wave 2 "riding to Wave 3" deferrals. It gets no dedicated test and needs none: `routingLabel` is module-private, every field the `*Rows()` builders project today IS in `ROUTING_LABELS` (so no fixture can reach the fallback without first adding an unmapped field to a builder), and the Spec-tab default-DOM pin in Step 3 already scans every `<dt>` in the card for `SNAKE_RE` — which is the assertion that would catch the drift this guard exists for. Say in the commit message that it is a guard against a future unmapped field, not a fix for an observed leak; a reviewer looking for the failing test it fixes should find that sentence instead.

5. Replace `routingValue` (`:51-69`) with a renderer that consults `specRouting` first. Note this deletes the SECOND and last hand-spelled routing-value `"discard"` literal in the frontend (`PipelineSpecView.tsx:52`; Task 3 converted the other, `SchemaFormTurn.tsx:74`); the replacement below uses the shared `DISCARD_CONNECTION` constant, so after this task the routing-value sentinel is spelled once and is greppable across the language boundary against `_producer_resolver.py:208`. The ~38 `ProposalEndpointKind` `"discard"` literals are a different vocabulary and are NOT in scope — see Task 3's `DISCARD_CONNECTION` ruling. Import it: `import { DISCARD_CONNECTION } from "@/lib/graphTopology";`.

```tsx
/** Fields whose value is an author-chosen NAME (not a connection, not an
 *  enum): rendered title-cased with the raw in `title`, same rule as ids. */
const AUTHOR_NAME_FIELDS: ReadonlySet<string> = new Set(["scope_name"]);

function RoutingDd({
  state,
  index,
  field,
  value,
}: {
  state: CompositionState;
  index: ConnectionIndex;
  field: string;
  value: unknown;
}): JSX.Element {
  if (value === DISCARD_CONNECTION) return <dd>dropped (recorded in the audit trail)</dd>;
  const phrase = routingPhrase(state, index, field, value);
  if (phrase !== null) return <dd title={phrase.raw}>{phrase.text}</dd>;
  if (AUTHOR_NAME_FIELDS.has(field) && typeof value === "string") {
    return <dd title={value}>{titleCaseLabel(value)}</dd>;
  }
  if (Array.isArray(value)) return <dd>{value.map(String).join(", ")}</dd>;
  return <dd>{displayValue(value)}</dd>;
}
```

and in `SpecSection` the routing loop (`:115-120`) becomes `<RoutingDd state={state} index={index} field={field} value={value} />` in place of `<dd>{routingValue(field, value)}</dd>`. Delete `routingValue` and its `elspeth-b9ebdf9011` comment (the `branches`-as-prose fix now lives in `specRouting.ts`, whose header carries the ticket reference).
6. Heading and kind (`:95`, `:103-104`):

```tsx
<h4 title={row.id}>{titleCaseLabel(row.id)}</h4>
...
<dd title={row.kind}>{titleCaseLabel(row.kind)}</dd>
```

   **`titleCaseLabel(row.id)`, NOT `row.label`.** `componentPhrase` resolves a plugin-less structural node to `descriptionLabel(node.description)`, and `PipelineSpecView.tsx:96-100` already renders `row.description` as the `<p className="pipeline-spec-step-description">` directly beneath the heading — so a described coalesce, row_union, queue, gate or collector (the norm for composer-authored graphs) would print *"Merge the two assessment branches"* twice, one line apart, and the card would show no name at all. Step 3 item 7 pins this. The routing `<dd>`s keep `componentPhrase`, where there is no sibling paragraph.

   The article `aria-label` (`:93`) stays `${singular} ${row.id}` — the accessible name is the identifier by design (exempted in the pin, and `<article>` maps to `role=article`, which supports naming from author, so an AT user hears "Node extract_invoice" while a sighted user reads "Extract Invoice").

Run: `npx vitest run src/components/workspace src/components/inspector` → PASS. Then `npx tsc --noEmit -p tsconfig.app.json && npx tsc --noEmit -p tsconfig.test.json` → clean.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/frontend/src/components/workspace/specRouting.ts src/elspeth/web/frontend/src/components/workspace/specRouting.test.ts src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.tsx src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.test.tsx
git commit -m "feat(spec-tab): routing values name the connected component, headings/kinds/policies in the reader register, raw ids in title (elspeth-93f5621f18)"
```

`elspeth-93f5621f18` closes at Task 11 with all three commits named (Task 1, Task 3's extraction, and this one).

---

### Task 5: Register batch (`elspeth-d74ab492dd`; absorbs the execution/consent half of `elspeth-59631ec7f7`)

One PR, one reviewer pass; commit per item so a reviewer can bisect — **and every step below now enumerates its own pathspecs**, because "Commit." with no `git add` is how the Task 3 / Task 9 / Task 10 staging gaps happened. Every item's acceptance: the raw token is absent from visible text and present in a **reachable** identifier home — an `.sr-only` span wired by `aria-describedby`, or a `<code>` secondary — with `title` as a sighted-mouse convenience alongside one of those rather than instead of them (see the egress ruling in Step 6 for why `title` alone is not a home). **`data-*` is NOT a reachable home and is not on this list**: it is invisible to every audience, which is why Task 1 uses `data-affected-node-id` as a deliberate FORENSIC/testing home and the plan says so there. Do not satisfy a Task 5 item with a `data-*` attribute.

**Order within each step: write the assertion first, watch it fail, then implement.** Steps 2–8 are stated implement-then-test in this document because the code is short and reads better beside its target — **that is a presentation order, not an execution order.** Every step must record a `→ FAIL` run before its `→ PASS` run, and the commit message names it. These are visible-copy edits where a test-after would still verify the right thing, so this is cheap insurance rather than ceremony; but a reviewer bisecting per-commit finds no red state to confirm against, and the plan's own header claims a failing-test-first cycle. Steps 1 (ModelChip) and 6 (egress) already record their red runs explicitly.

**Files:**
- Create: `src/elspeth/web/frontend/src/components/chat/modelDisplayName.ts` + `.test.ts`
- Modify: `src/elspeth/web/frontend/src/components/catalog/pluginDisplayName.ts` (`ACRONYMS`: declaration `:23`, members `:24-38`, close `:39`)
- Modify: `src/elspeth/web/frontend/src/components/chat/ModelChip.tsx:24-30`, `ModelChip.test.tsx:19-34`
- Modify: `src/elspeth/web/frontend/src/components/settings/SecretsPanel.tsx:20-34` (`ScopeBadge`), `SecretsPanel.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/chat/AcknowledgementCard.tsx:711-716`, `AcknowledgementCard.test.tsx:510-525`
- Create: `src/elspeth/web/frontend/src/components/execution/diagnosticPhrases.ts`
- Modify: `src/elspeth/web/frontend/src/components/execution/RunsHistoryDrawer.tsx:209-264` (`RunStateFailureDetail`), `RunsHistoryDrawer.test.tsx:376-475`, `components/execution/execution.css` (comment `:145-148`, rule opens `:149`)
- Modify: `src/elspeth/web/frontend/src/components/audit/ExplainDialog.tsx:120-122`, `components/audit/audit.css` (comment block `:347-365`, rule `:366-373`), `ExplainDialog.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.tsx` (`buildRunEgressSummary` `:180-286`, render `:694-700`, and the `.sr-only` + `aria-describedby` pattern at `:668-681` this step reuses), `ExecuteButton.test.tsx` (16 `buildRunEgressSummary(` call sites, dialog pins `:356-360`, local `makeComposition` `:99-113`)
- Modify: `src/elspeth/web/frontend/src/components/chat/InlineSourceCreatedTurn.tsx:190-193`, `InlineSourceCreatedTurn.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/chat/guided/ComponentReviewTurn.tsx:76-84`, `ComponentReviewTurn.test.tsx:43-44,:89-90,:100-101`

**Interfaces:**
- Produces: `modelDisplayName(modelId: string): string`; `RunEgressLine { text: string; identifiers: string }` and `buildRunEgressSummary(...): RunEgressLine[]` (same parameters as today, `:180-186`); `DIAGNOSTIC_REASON_PHRASES`, `DIAGNOSTIC_CAUSE_PHRASES` (**OPEN** `ReadonlyMap<string, string>` — see the ruling below; a pre-fix draft called them "closed", which is a contradiction in terms and contradicts Task 4's own ruling that a `Map` closes against nothing); `INLINE_SOURCE_PROVENANCE_LABELS: Record<InlineSourceProvenance, string>`.
- Consumes: `pluginDisplayName`, `titleCaseLabel`, `stepLabelForNodeId`, `MarkdownRenderer` (`chat/MarkdownRenderer.tsx:68`, prop `content: string`).

**Ruling — `modelDisplayName` DERIVES the label; the ticket's "from the profile/catalog" is not available and this records the deviation.** `elspeth-d74ab492dd` says "Show a display name from the profile/catalog; raw id in title." There is no such catalog to read: the frontend holds `composerModel` as a bare `string` (`sessionStore.ts:1123`) and no endpoint supplies model labels, so a derivation from the id is the only route open — the ticket's premise was aspirational, not a source that exists. The `title` half of the ticket is honoured exactly. Cost if wrong, and it is a real cost worth naming rather than hiding: a vendor whose own casing differs from title case is rendered wrong — `"gpt-5.5"` becomes "GPT 5.5" where the vendor writes "GPT-5.5". The raw id is one hover away, and the fix if a real label source ever lands is to swap the derivation for the lookup behind the same function name.

- [ ] **Step 1 (ModelChip): failing test, then implement**

`modelDisplayName.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { modelDisplayName } from "./modelDisplayName";

describe("modelDisplayName", () => {
  it("takes the leaf of a provider path and title-cases hyphenated words", () => {
    expect(modelDisplayName("openrouter/anthropic/claude-sonnet-4.6")).toBe("Claude Sonnet 4.6");
    expect(modelDisplayName("anthropic/claude-sonnet-5")).toBe("Claude Sonnet 5");
  });
  it("upper-cases GPT through the shared acronym set", () => {
    expect(modelDisplayName("gpt-5.5")).toBe("GPT 5.5");
  });
  it("returns a bare id unchanged apart from casing", () => {
    expect(modelDisplayName("sonnet")).toBe("Sonnet");
  });
});
```

`modelDisplayName.ts`:

```ts
// ============================================================================
// modelDisplayName — reader-register label for a composer/LLM model id
// (elspeth-d74ab492dd). Model ids are provider paths
// ("openrouter/anthropic/claude-sonnet-4.6"); the leaf segment is the model,
// the hyphens are word breaks. Casing goes through the ONE title-caser
// (catalog/pluginDisplayName.ts) so "gpt" reads "GPT" here as everywhere.
// Presentation only — the raw id stays in `title` and on the wire.
// ============================================================================

import { titleCaseLabel } from "@/components/catalog/pluginDisplayName";

export function modelDisplayName(modelId: string): string {
  const leaf = modelId.slice(modelId.lastIndexOf("/") + 1);
  // The `-` -> ` ` pre-split is DELIBERATE and local. titleCaseLabel splits on
  // /[_\s]+/ (pluginDisplayName.ts:88), so hyphens are not word breaks there
  // and titleCaseLabel("claude-sonnet-4.6") yields "Claude-sonnet-4.6". Widening
  // that regex to /[_\s-]+/ would change every hyphenated author-chosen node id
  // across the frontend, which is a blast radius this wave has not measured.
  // Casing itself still goes through the ONE title-caser, so this is a second
  // word-SPLITTING rule, not a second title-caser.
  return titleCaseLabel(leaf.replace(/-/g, " "));
}
```

`pluginDisplayName.ts:24-39` — add `"gpt",` to `ACRONYMS` (alphabetical, after `"db"`). Ruling: the set is the frontend's single acronym list (`titleCaseLabel`'s docblock, `:76-79`) and no plugin id contains the word — verify with `grep -rn '"gpt' src/elspeth/plugins --include=*.py` from the **repository root** (that pathspec is repo-root-relative, unlike the frontend-scoped grep Task 3 had to re-run; it returns nothing). So no catalog card changes. Also checked: `pluginDisplayName.test.ts:35-53` SAMPLES acronyms rather than enumerating or snapshotting the set, so adding a member breaks nothing there. Cost if wrong: a future plugin named `gpt_something` would card as "GPT Something" — correct anyway.

**Ruling — DELETE `ModelChip`'s `aria-label` rather than rewriting it, and un-hide "Composer:".** `ModelChip.tsx:25` puts `aria-label` on a bare `<span className="chat-model-chip">` — a role-less element, i.e. `role=generic`, where ARIA 1.2 prohibits the attribute and assistive technology ignores it. **This repo has already ruled on exactly this defect and fixed it once:** `components/catalog/PluginCard.tsx:190-192` carries the comment *"`role="group"` so the aria-label is actually exposed — aria-label on a role-less div (role=generic) is ignored by AT (WCAG 1.3.1, elspeth-37293a3b7c)."* The aggravating factor here is that the chip's only context word, "Composer:", is `aria-hidden="true"` at `:26-28` — so if the prohibited label is dropped (which is what conforming AT does), the user hears a bare model name with nothing saying what it names.

A pre-fix draft rewrote the label's *text* into the reader register and left the exposure defect standing — improving a string nobody hears, in a wave whose entire subject is what reaches the user, on lines it was already rewriting. The fix is one deletion and one deletion:

```tsx
  return (
    <span className="chat-model-chip" title={model}>
      <span className="chat-model-chip-label">Composer:</span>{" "}
      {modelDisplayName(model)}
    </span>
  );
```

The chip now reads "Composer: Claude Sonnet 4.6" as ordinary text, to every audience, with no ARIA at all and nothing for a future reviewer to re-litigate. **A `role` on the outer span is the weaker alternative and is rejected:** `role="img"` would make the children presentational, so the display name would stop being separately readable, and `elspeth-37293a3b7c`'s `role="group"` precedent was for a *container of chips*, which does not transfer to a single text chip. Cost if wrong: the visible text gains the word "Composer:" for screen-reader users who previously heard a synthesised label — which is the correction, not a regression.

**Contrast worth keeping in view, because the same attribute is right one step away:** Step 8 puts `aria-label` on an `<li>` in `ComponentReviewTurn.tsx:76-84`. `listitem` **does** support naming from author, so that one is permitted and stays.

`ModelChip.test.tsx:19-34`: **delete** the `getByLabelText("Composer model: anthropic/claude-sonnet-4.6")` assertion — there is no aria-label to query, and replacing it with a label assertion would re-ratify the defect. Replace it with `expect(screen.getByText("Composer:")).toBeInTheDocument();` (the prefix is now announced rather than hidden). Then `getByText("anthropic/claude-sonnet-4.6")` → `getByText("Claude Sonnet 4.6")`; add `expect(screen.getByTitle("anthropic/claude-sonnet-4.6")).toBeInTheDocument();` and `expectNoIdentifiersInDefaultDom(container)` on a render with `composerModel: "openrouter/anthropic/claude-sonnet-5"`.

Run: `npx vitest run src/components/chat/modelDisplayName.test.ts src/components/chat/ModelChip.test.tsx src/components/catalog/pluginDisplayName.test.ts` → PASS. Commit:

```bash
git add src/elspeth/web/frontend/src/components/chat/modelDisplayName.ts \
        src/elspeth/web/frontend/src/components/chat/modelDisplayName.test.ts \
        src/elspeth/web/frontend/src/components/catalog/pluginDisplayName.ts \
        src/elspeth/web/frontend/src/components/chat/ModelChip.tsx \
        src/elspeth/web/frontend/src/components/chat/ModelChip.test.tsx
git commit -m "feat(chat): ModelChip shows the model's display name, raw id in title; aria-label on a role-less span deleted (elspeth-d74ab492dd, elspeth-37293a3b7c)"
```

- [ ] **Step 2 (ScopeBadge): implement + test**

`SecretsPanel.tsx:20-34`:

```tsx
const SCOPE_LABELS: Record<SecretInventoryItem["scope"], string> = {
  user: "Yours",
  server: "Deployment",
  org: "Organisation",
};

function ScopeBadge({ scope }: { scope: SecretInventoryItem["scope"] }) {
  const colors: Record<SecretInventoryItem["scope"], { bg: string; text: string }> = {
    user: { bg: "var(--color-accent-muted)", text: "var(--color-accent)" },
    server: { bg: "var(--color-info-bg)", text: "var(--color-info)" },
    org: { bg: "var(--color-surface-raised)", text: "var(--color-text-secondary)" },
  };
  const { bg, text } = colors[scope] ?? colors.org;
  return (
    <span
      className="secrets-scope-badge"
      style={{ backgroundColor: bg, color: text }}
      title={scope}
    >
      {SCOPE_LABELS[scope] ?? scope}
    </span>
  );
}
```

Add to `SecretsPanel.test.tsx` (the `beforeEach` at `:17-40` seeds one `user` and one `server` secret):

```tsx
  it("names secret scopes in the reader register with the raw scope in title (elspeth-d74ab492dd)", () => {
    render(<SecretsPanel onClose={vi.fn()} />);
    expect(screen.getByText("Yours")).toHaveAttribute("title", "user");
    expect(screen.getByText("Deployment")).toHaveAttribute("title", "server");
    // Anchored: `^user$` rather than a substring, because the same fixture
    // also carries `source_kind: "user"` on the secret. An unanchored
    // queryByText("user") would fail for an unrelated reason if source_kind
    // ever reached visible text — a negative that reports the wrong defect.
    expect(screen.queryByText(/^user$/)).not.toBeInTheDocument();
  });
```

**Note, pre-existing and NOT worsened by this step:** `SecretsPanel.test.tsx:17-40` does `useSecretsStore.setState({ secrets: [...], isLoading, error, loadSecrets: vi.fn() })` in a bare `beforeEach` — it replaces a store METHOD with a mock and never restores it, and this is the only file this wave adds a test to that lacks a `resetStore` (checked across `ModelChip.test.tsx`, `PipelineSpecView.test.tsx`, `SecretsPanel.test.tsx`, `PluginCard.test.tsx`, `AcknowledgementStack.test.tsx`, `FilterChipStrip.test.tsx` — only `SecretsPanel.test.tsx` is missing it). The new test reads that seed and mutates nothing, so it neither depends on nor worsens the leak; adding `beforeEach(() => resetStore(useSecretsStore))` here would fight the file's own `setState` seeding and is not this step's job. Recorded so the next wave does not re-derive it. The Global Constraint's reset obligation is correspondingly widened from "becomes a flag reader in this wave" to "any test file this wave adds a store-MUTATING test to" — which this is not.

Run: `npx vitest run src/components/settings/SecretsPanel.test.tsx` → PASS. Commit:

```bash
git add src/elspeth/web/frontend/src/components/settings/SecretsPanel.tsx src/elspeth/web/frontend/src/components/settings/SecretsPanel.test.tsx
git commit -m "feat(settings): secret scopes read Yours/Deployment/Organisation, raw scope in title (elspeth-d74ab492dd)"
```

- [ ] **Step 3 (amendment cap): implement + test**

`AcknowledgementCard.tsx:711-716`:

```tsx
          {amendIsTooLong && (
            // Characters, not bytes, in the sentence the writer reads. The
            // byte overage is an UPPER bound on the characters to remove
            // (multibyte text shortens faster), so "about" is honest.
            //
            // The exact figures go in an .sr-only span, NOT in `title` alone.
            // This <p> is not focusable, so a keyboard user gets no hover and
            // no focus tooltip; and `title` on a role="status" element is a
            // naming fallback, not part of the live-region announcement, so a
            // screen-reader user would hear only the approximate count. The
            // .sr-only span is inside the live region and is announced with it.
            <p className="ack-card-amend-cap-warning" role="status">
              Shorten this by about{" "}
              {amendByteLength - INTERPRETATION_AMENDMENT_MAX_BYTES} characters
              to fit the {INTERPRETATION_AMENDMENT_MAX_BYTES / 1024} KB limit.
              <span className="sr-only">
                {" "}({amendByteLength} bytes; the maximum is{" "}
                {INTERPRETATION_AMENDMENT_MAX_BYTES} bytes.)
              </span>
            </p>
          )}
```

`AcknowledgementCard.test.tsx:523`: `expect(screen.getByText(/8192 bytes/)).toBeTruthy();` →

```tsx
    const warning = screen.getByRole("status");
    expect(warning).toHaveTextContent("Shorten this by about 8 characters to fit the 8 KB limit.");
    // Exact figures reachable by every audience, not hover-only.
    expect(warning).toHaveTextContent("(8200 bytes; the maximum is 8192 bytes.)");
```

(8200 ASCII `a`s = 8200 bytes; 8200 − 8192 = 8; 8192/1024 = 8 KB. `role="status"` appears exactly once in `AcknowledgementCard.tsx` — `:712` — so `getByRole("status")` is unambiguous.)

**`.sr-only` is an existing utility class in this codebase** (`ExecuteButton.tsx:678` uses it for exactly this purpose); it needs no new stylesheet rule and therefore does not touch the `classNames.test.ts` whole-tree gate.

**Not a Wave 3 regression, recorded so it is not attributed here:** the `role="status"` live region already re-announces on every keystroke while over the cap — the current text at `:712-714` also carries a changing byte count. The chattiness is pre-existing; this step changes the words, not the cadence.

**This step's commit is the ONE that cannot use a whole-path `git add`** — `AcknowledgementCard.tsx` is the file Task 1 also edits (`:597-604`, the `data-affected-node-id` `<section>`), and the shared-file ruling in Sequencing requires Task 1 to land first. If Task 1 has landed and its edit is committed, a plain `git add` is safe and correct. **If the two lanes overlapped at all, stage this hunk alone:**

```bash
# Task 1 landed and committed -> plain add:
git add src/elspeth/web/frontend/src/components/chat/AcknowledgementCard.tsx src/elspeth/web/frontend/src/components/chat/AcknowledgementCard.test.tsx
# Lanes overlapped -> stage only the amendment-cap hunk (:711-716) and say so
# in the commit message, per the shared-file ruling:
git add -p src/elspeth/web/frontend/src/components/chat/AcknowledgementCard.tsx
git add src/elspeth/web/frontend/src/components/chat/AcknowledgementCard.test.tsx
git commit -m "feat(chat): amendment cap warning in characters, exact bytes in an sr-only span (elspeth-d74ab492dd)"
git status --short   # AcknowledgementCard.tsx may remain dirty if Task 1's hunk is unlanded — that is expected, not a leftover
``` Run: `npx vitest run src/components/chat/AcknowledgementCard.test.tsx` → PASS. Commit.

- [ ] **Step 4 (failure enums): closed phrase map + row**

`diagnosticPhrases.ts`:

```ts
// ============================================================================
// diagnosticPhrases — reader-register phrases for the CLOSED diagnostic
// enums the curated failure row renders (elspeth-d74ab492dd). Known values
// read as prose with the raw enum in `title`; anything not listed here stays
// in <code> — an unknown identifier must never be dressed up as a sentence.
// Maps, not objects, so a value named "constructor" cannot collide with
// Object.prototype. Add an entry only for a value ELSPETH itself emits
// (grep the backend for the literal before adding).
//
// These maps are OPEN, and that is the honest choice here — unlike Task 4's
// POLICY_PHRASES, which close against a union. types/index.ts declares no
// union for run-failure reason or cause; RunsHistoryDrawer.tsx:183 reads them
// through safeDiagnosticIdentifier, i.e. they are free-form wire strings. With
// no member set to close against, a closed Record would mean inventing the set
// here, which is the thing this wave is correcting. The unknown arm is not a
// silent degradation either: it renders <code>, which is the correct register
// for an identifier nobody has phrased.
//
// The repo's precedent for a phrase map that genuinely CLOSES is
// components/execution/runTerminalPhrases.ts:43,
// `const TERMINAL_RUN_PHRASES: Record<TerminalRunStatus, TerminalRunPhrase>`,
// whose docblock states the property: adding a status "fails to compile here
// until its phrasing is decided." Use that shape when a union exists.
//
// Axis note (no duplication): runTerminalPhrases.ts phrases run TERMINAL
// STATUSES, and InlineRunResults.tsx:128/:141 phrase DISCARD stages and
// causes. This module phrases run-FAILURE reason/cause. Three axes, three
// homes; check here first before adding a fourth.
// ============================================================================

export const DIAGNOSTIC_REASON_PHRASES: ReadonlyMap<string, string> = new Map([
  ["submit_failed", "the request could not be submitted"],
]);

export const DIAGNOSTIC_CAUSE_PHRASES: ReadonlyMap<string, string> = new Map([
  ["s3_object_unreadable", "the S3 object could not be read"],
  ["provider_rejected", "the provider rejected the request"],
]);
```

(These three are the values the existing tests pin — `RunsHistoryDrawer.test.tsx:386-389,:430-447`. Before adding more, grep `src/elspeth` for the literal; do not invent phrases for enums that are not emitted.)

`RunsHistoryDrawer.tsx` — add a small renderer above `RunStateFailureDetail` and use it for the two rows (`:252-261`); the `label` (`code ?? errorType ?? reason`, a provider error CLASS such as `InvalidS3ObjectException`) stays in `<code>` — it is an identifier by nature:

```tsx
function DiagnosticValue({
  value,
  phrases,
}: {
  value: string;
  phrases: ReadonlyMap<string, string>;
}): JSX.Element {
  const phrase = phrases.get(value);
  return phrase === undefined ? <code>{value}</code> : <span title={value}>{phrase}</span>;
}
```

```tsx
      {failure.reason && (
        <div>
          Reason: <DiagnosticValue value={failure.reason} phrases={DIAGNOSTIC_REASON_PHRASES} />
        </div>
      )}
      {failure.cause && (
        <div>
          Cause: <DiagnosticValue value={failure.cause} phrases={DIAGNOSTIC_CAUSE_PHRASES} />
        </div>
      )}
```

Import both maps from `./diagnosticPhrases`. Tests: `:406-407` → `toHaveTextContent("Reason: the request could not be submitted")`, `toHaveTextContent("Cause: the S3 object could not be read")`, plus `expect(within(failure).getByText("the request could not be submitted")).toHaveAttribute("title", "submit_failed")`; `:432` `toHaveTextContent("Cause: provider_rejected")` → `"Cause: the provider rejected the request"`; `:455` (`"failed - submit_failed"` — here `submit_failed` is the LABEL because code/error_type were rejected; the label stays `<code>`) unchanged. The default-DOM pin at `:636+` already covers this drawer; keep it green.

`components/execution/execution.css` (comment `:145-148`, rule opens `:149`): replace only the stale first sentence. "Per-STATE failure provenance inside the diagnostics disclosure — …" → "Per-STATE failure provenance, rendered at one site regardless of the detail level (hoisted out of the disclosure by elspeth-34e810312c) — the same error family as .run-failure-detail (header.css:783), one register quieter because it nests under the per-run detail."

**The sentence "Previously rule-less, so the recorded failure read as ordinary diagnostics prose." (`:147-148`) STAYS.** It records why the rule exists at all, which the new first sentence does not carry. Only the "inside the diagnostics disclosure" claim is stale, and the reason it is stale is verifiable: `RunStateFailureDetail` is rendered at `RunsHistoryDrawer.tsx:534-544`, **outside** both `showAdvanced`-gated `.run-failure-detail` blocks (`:517-531`). This is a comment edit, not a rule edit — do not touch the declarations.

Run: `npx vitest run src/components/execution` → PASS. Commit:

```bash
git add src/elspeth/web/frontend/src/components/execution/diagnosticPhrases.ts \
        src/elspeth/web/frontend/src/components/execution/RunsHistoryDrawer.tsx \
        src/elspeth/web/frontend/src/components/execution/RunsHistoryDrawer.test.tsx \
        src/elspeth/web/frontend/src/components/execution/execution.css
git commit -m "feat(run-history): curated failure row names known reason/cause in prose, unknown stays <code> (elspeth-d74ab492dd)"
```

- [ ] **Step 5 (ExplainDialog): Markdown, not `<pre>`**

`ExplainDialog.tsx:120-122`:

```tsx
          {explain && (
            <div className="explain-dialog-narrative">
              <MarkdownRenderer content={explain.narrative} />
            </div>
          )}
```

Import: `import { MarkdownRenderer } from "@/components/chat/MarkdownRenderer";`.

**`components/audit/audit.css` — THREE edits, named by line, because the comment block asserts the very declaration being deleted.** The comment runs `:347-365` and the rule `:366-373`:

1. **`:347` first sentence** → "Narrative prose rendered through MarkdownRenderer (elspeth-d74ab492dd). It was a `<pre>`; the producer (`src/elspeth/web/audit_readiness/explain.py`) emits an intro line, a blank line, then consecutive `- `-prefixed sentences — i.e. a markdown LIST — so the renderer now gives screen-reader users a list role, an item count and list navigation that a `<pre>` cannot provide."

   **Do NOT write "was a `<pre>`, which screen readers announce as code."** A pre-fix draft did, and it is factually wrong: `<pre>` has no ARIA role mapping (it is generic); `<code>` / `role="code"` is what is announced, and even that inconsistently. The change is a genuine WCAG 1.3.1 improvement — for the list reason above, which is the one worth writing down.

2. **`:358-360` MUST be replaced, not left standing.** It currently reads *"`white-space: pre-wrap` stays: the narrative's own line breaks are meaningful, and now they wrap inside a bounded column instead of the viewport."* After this step the declaration is gone and that paragraph asserts both a declaration the file no longer has and a rationale this change reverses. Replace it with one sentence: "`white-space: pre-wrap` is gone: the renderer now owns block structure, so paragraphs and list items carry the line breaks that the declaration used to preserve."

3. **`:362-365` — delete the "Still owed …" paragraph.** It describes this exact change as outstanding; this step is what closes it.

Keep `max-width: 74ch` and its measure-cap paragraph (`:354-357`), the sans family/size and `line-height`. Delete only `white-space: pre-wrap;` from the declarations.

`ExplainDialog.test.tsx:26-41`: the two `findByText`/`getByText` regexes still match (each line is its own paragraph). Add two tests — **and note that both fixtures below are shaped from the REAL producer, not invented.** A pre-fix draft used `\n\n`-separated paragraphs and a `3 * 2` string, and neither models what `explain.py` emits: `build_narrative` (`src/elspeth/web/audit_readiness/explain.py:16-58`) joins with `"\n"` (single newline, `:58`) and the body is **consecutive `- `-prefixed lines with no blank line between them** (`:29`, `:33`, `:37`, `:40`, `:44`), plus a `"Web plugin policy readiness:"` line at `:43` immediately followed by more `- ` rows. That single-newline + `- ` combination is precisely what `white-space: pre-wrap` was protecting and precisely what a `\n\n` fixture cannot detect.

```tsx
  it("renders the real narrative shape as prose and a LIST, not preformatted text (elspeth-d74ab492dd)", async () => {
    // Fixture shaped from build_narrative (explain.py:22-58): intro line,
    // blank line, then consecutive "- " rows with NO blank line between them,
    // then a blank line and a retention paragraph. This is the shape the
    // pre-wrap deletion actually risks.
    vi.mocked(api.fetchAuditReadinessExplain).mockResolvedValueOnce({
      session_id: SESSION_ID,
      composition_version: 1,
      narrative:
        "When you run this pipeline, ELSPETH will record:\n\n"
        + "- Source data — each row from the CSV input. SHA-256 hash recorded.\n"
        + "- Output results — written to CSV.\n\n"
        + "Retention: 90 days by default.",
    });
    const { container } = render(<ExplainDialog sessionId={SESSION_ID} compositionVersion={1} onClose={() => {}} />);
    await screen.findByText(/When you run this pipeline/);
    expect(container.querySelector("pre")).toBeNull();
    // Both bullets survive as SEPARATE list items — the assertion that a
    // collapsed line pair would fail. This is the a11y win the CSS comment
    // now claims: a list role and an item count a <pre> cannot provide.
    const items = container.querySelectorAll(".explain-dialog-narrative li");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("Source data");
    expect(items[1]).toHaveTextContent("Output results");
    expect(container.querySelectorAll(".explain-dialog-narrative p")).toHaveLength(2);
  });

  it("passes identifiers and markdown-active punctuation through unaltered (audit fidelity)", async () => {
    // An audit narrative is generated prose that may contain node ids and
    // literal punctuation. Markdown TRANSFORMS text: `_wrapped_` becomes
    // emphasis, a leading `#` becomes a heading. Moving this surface from
    // <pre> to a markdown renderer is only safe if the narrative survives it,
    // and a fidelity failure is a finding worth having before this lands.
    //
    // The tokens here are chosen to be markdown-ACTIVE. A pre-fix draft used
    // "3 * 2" and an intraword underscore, both of which are INERT in GFM
    // (a space-surrounded `*` is not emphasis; an intraword `_` is not
    // emphasis) — so that test passed whether or not fidelity held.
    vi.mocked(api.fetchAuditReadinessExplain).mockResolvedValueOnce({
      session_id: SESSION_ID,
      composition_version: 1,
      narrative: "# Retention\n\n_wrapped_ and node submit_failed closed the sink.",
    });
    const { container } = render(<ExplainDialog sessionId={SESSION_ID} compositionVersion={1} onClose={() => {}} />);
    await screen.findByText(/submit_failed/);
    const narrative = container.querySelector(".explain-dialog-narrative");
    // submit_failed survives verbatim: GFM does not open emphasis on an
    // intraword underscore. This is the assertion that matters for ids.
    expect(narrative?.textContent).toContain("node submit_failed closed the sink.");
    // The two markdown-active tokens ARE transformed, and this test PINS that
    // rather than asserting it away — see the transformation ruling below.
    expect(narrative?.querySelector("h1")).not.toBeNull();
    expect(narrative?.querySelector("em")?.textContent).toBe("wrapped");
  });
```

**Ruling — markdown transformation of the narrative is ACCEPTED and pinned, not prevented; mermaid is the one exception and it is unreachable.** Three transformation risks were raised and each gets an answer rather than a hedge:

- **Emphasis and headings.** `build_narrative` is deterministic Python (`explain.py:3-4`), not an LLM call. It emits no `#` heading and no `*`/`_` emphasis today — every line is a plain sentence or a `- ` row — so the second test's `h1`/`em` assertions describe a hypothetical payload, and they are written as *pins on the renderer's behaviour* so that if `explain.py` ever starts emitting a `#`, this test tells the next reader exactly what will happen to it. **What must never break is the identifier case**, which is the first assertion and is the one that reflects real content (`submit_failed`, `plugin_trust`, plugin ids): GFM does not open emphasis on an intraword `_`, so ids survive.
  - One real-but-currently-unreachable consequence, recorded rather than fixed: a narrative containing a `#` would inject an `<h1>` into a dialog titled by an `<h2>` (`ExplainDialog.tsx:96-98`) — a heading-order defect. It cannot occur while `explain.py` is the only producer. If a producer ever emits headings, the fix is a `components` override on `MarkdownRenderer`, not a change here.
- **Raw HTML: no XSS vector.** `MarkdownRenderer.tsx:68-80` is `react-markdown` + `remark-gfm` with `a: SafeLink` and `code: CodeBlock`, and **no `rehype-raw`** — raw HTML is escaped.
- **Mermaid: `MarkdownRenderer` renders ` ```mermaid ` fences as interactive diagrams by design.** An interactive diagram inside an audit-readiness narrative is a new behaviour on an audit surface, and it is the one transformation that would be wrong. **Ruling: accept it, because it is unreachable and blocking it costs more than it buys.** `build_narrative` emits no code fence of any kind — it has no `\`\`\`` anywhere (`explain.py:22-57`) — and the narrative is the only thing rendered here. Adding a fence-stripping wrapper would put a second, divergent markdown policy in the tree to defend against a producer that cannot produce it. **If a future producer for this surface can emit fenced blocks, that producer's ticket owns the decision, and this ruling is where to start.** Cost if wrong: an audit dialog renders a diagram somebody's generator emitted; the raw text is still in the DOM as the fence's content, so nothing is hidden.

**No mermaid mock is needed.** `MarkdownRenderer.test.tsx` contains no `vi.mock` at all (verified — zero hits in that file, and no global mock in `src/test/setup.ts`), and its "renders a mermaid container" test at `:56` passes against the real `mermaid` import: `mermaid.initialize()` runs fine in jsdom. A pre-fix draft of this step told the implementer to copy a mocking pattern from that file that does not exist there. If the import does trip during implementation, that is new information — diagnose it then, do not mock `MarkdownRenderer` itself (the list-item and paragraph counts are the assertions).

Run: `npx vitest run src/components/audit/ExplainDialog.test.tsx src/styles/classNames.test.ts` → PASS. Commit:

```bash
git add src/elspeth/web/frontend/src/components/audit/ExplainDialog.tsx \
        src/elspeth/web/frontend/src/components/audit/ExplainDialog.test.tsx \
        src/elspeth/web/frontend/src/components/audit/audit.css
git commit -m "feat(audit): explain narrative renders through MarkdownRenderer, not <pre> (elspeth-d74ab492dd)"
```

- [ ] **Step 6 (egress lines): reader text + identifier title**

`ExecuteButton.tsx` — above `buildRunEgressSummary` (`:180`):

```ts
export interface RunEgressLine {
  /** Reader register: step labels and plugin display names. */
  text: string;
  /** Identifier register — the exact sentence this dialog showed before
   *  Wave 3, unchanged so every component and plugin is still named by id
   *  (R2-F7). Surfaced via an `.sr-only` span wired with `aria-describedby`
   *  on the line, NOT via `title` alone — `title` is not reliably announced
   *  and an <li> is not focusable, so it is a mouse convenience beside the
   *  span, never the only route (see :668-676 in this file). */
  identifiers: string;
}

type EgressRegister = "reader" | "identifier";
```

Refactor the body into `function egressSentences(register: EgressRegister, compositionState, catalogTransforms, catalogLoadFailed, catalogSources, catalogIsLoading): string[]` — the existing function body verbatim, with these three label sites switched on `register`:

```ts
  const component = (id: string): string =>
    register === "identifier" ? id : (stepLabelForNodeId(compositionState, id) ?? titleCaseLabel(id));
  const plugin = (pluginId: string): string =>
    register === "identifier" ? pluginId : pluginDisplayName(pluginId);
  const model = (modelId: string): string =>
    register === "identifier" ? modelId : modelDisplayName(modelId);
```

- ordinary sources (`:202`): `` `${sourceComponentId(sourceName)} (${source.plugin})` `` → `` `${register === "identifier" ? sourceComponentId(sourceName) : component(sourceName)} (${plugin(source.plugin)})` `` (`stepLabelForNodeId` resolves sources by their KEY, `interpretationStepLabel.ts:106-107`, so pass `sourceName`, not the `source:` composite);
- LLM sources (`:210`): same substitution for the name; **`llmSourceBindingLabel(source)` is unchanged — see the LLM-source ruling below, which makes that a decision rather than an omission**;
- LLM nodes (`:225-228`): `` `${component(node.id)} (model ${model(node.options.model)})` `` / `component(node.id)`;
- network / unverifiable nodes — **four sites, at `:242`, `:253`, `:256` and `:265`** (re-verified; a pre-fix draft cited `:240,:255,:258,:268`, which land on a filter predicate, a filter predicate, a bare comment line and the unrelated `if (networkNodes.length > 0) {` check): `` `${component(node.id)} (${plugin(node.plugin as string)})` ``. All four are the same literal, so grep for `` `${node.id} (${node.plugin})` `` rather than trusting the line list, and expect exactly four hits;
- outputs (`:279`, map opens `:278`): `` `${component(output.name)} (${plugin(output.plugin)})` ``.

**Ruling — `modelDisplayName` must NOT strip the provider from the run-confirm egress sentence. The egress line keeps the full path; only `ModelChip` gets the leaf-only form.** `modelDisplayName` takes the leaf segment, so `"openrouter/anthropic/claude-sonnet-4.6"` renders "Claude Sonnet 4.6". On a header chip that is right. On `ExecuteButton.tsx:225-231` it is not: that is the **run-confirm consent dialog**, the sentence a user reads before authorising rows to leave the deployment, and **the model id's provider path IS the egress destination** — `openrouter` is the host that receives the data and `anthropic` is the vendor. Dropping them leaves "Sends rows to the configured LLM: Classify (model Claude Sonnet 4.6)." with the recipient named nowhere in visible text.

**The operative clause is "(every sentence; R2-F7 must not reappear)", not "it never hides one" — and getting that right matters because later waves cite these rulings.** Read strictly, "it never hides one" has "audit-required **elements**" as its antecedent and is scoped to `show_advanced` gating: it is an ELEMENT-level commitment (do not remove a listed element from the default DOM), and stretching it to a WORD-level question about content inside a sentence that remains present is an extension, not an application. The parenthetical **does** carry the weight directly: "R2-F7 must not reappear" names a defect CLASS — R2-F7 was an under-disclosure defect on this exact surface — and it is not scoped to `show_advanced` at all. **Dropping the egress host from the visible consent sentence is under-disclosure on the run-confirm dialog, which that clause prohibits outright.** "It never hides one" is consistent with the conclusion but is not its source. So this is not an operator judgement call, it is the constraint applied — via the right limb. A pre-fix draft weighed only the *casing* of the leaf (`"gpt-5.5"` → "GPT 5.5") and never noticed the segments before it were discarded. The new whole-dialog pin cannot catch it either: it asserts the ABSENCE of identifiers, so a required disclosure going missing reads as success.

Implementation — the `model` helper's reader arm keeps the path and phrases only the leaf:

```ts
  const model = (modelId: string): string => {
    if (register === "identifier") return modelId;
    const cut = modelId.lastIndexOf("/");
    // The provider path IS the egress destination on this surface. Phrase the
    // model, keep the route (elspeth-59631ec7f7 / R2-F7).
    return cut === -1
      ? modelDisplayName(modelId)
      : `${modelDisplayName(modelId)} via ${modelId.slice(0, cut)}`;
  };
```

so the sentence reads "Classify (model Claude Sonnet 4.6 via openrouter/anthropic)". `modelDisplayName` itself is unchanged and `ModelChip` keeps the leaf-only form — a header chip is not a consent surface. Cost if wrong: the consent sentence is a few words longer than the chip. That is the correct side to err on for a disclosure.

**A provider segment is an identifier-surface value carried in prose BY DESIGN, and the resolution if one contains an underscore is a scoped `allowSelectors`, never dropping the segment.** This ruling puts `modelId.slice(0, cut)` into the dialog's **visible text**, and the same step adds `expectNoIdentifiersInDefaultDom` over the whole dialog, whose `SNAKE_RE` is `/\b[a-z]+_[a-z_]+\b/`. The plan's fixture (`openrouter/anthropic`) has no underscore and the pin passes. But `azure_openai/…` or `bedrock_runtime/…` is plausible for this deployment, and such a fixture would turn this task's own new pin red with the PRODUCT behaviour correct and the GATE wrong. **Resolve it by wrapping the segment and exempting the wrapper — never by dropping the provider segment or weakening `SNAKE_RE`.** Be concrete, because "a narrow `allowSelectors`" has no narrow implementation: `allowSelectors` works by `clone.querySelectorAll(selector).forEach((el) => el.remove())` (`defaultDomPins.ts:41-45`), i.e. it removes whole ELEMENTS from the text scan. There is no way to exempt a substring of a sentence, so the segment needs an element of its own:

```tsx
… (model {modelDisplayName(id)} via <span className="egress-provider">{providerPath}</span>)
```

exempted as `allowSelectors: [".run-disclosure-summary .egress-provider"]`. **This does not collide with the "do not add a class for a test" rule below**, and the distinction is worth stating: `egress-provider` is PRODUCT markup — the provider path is a distinct semantic span in the consent sentence, and giving it a class is how it would be written even with no test in the tree (it is also where any future styling of the destination would hang). The rule below forbids inventing a class whose only reason to exist is a selector for a pin. A new class does need a real stylesheet rule or a `classNames.test.ts` allowlist entry with a stated reason — that is the whole-tree gate, and it applies here like anywhere else. Naming the resolution here rather than leaving it to an implementing lane under time pressure is the whole point: this is the exact mirror of the LLM-source alias case below, and that one already has a ruling. Both halves of the same question now do.

**Ruling — LLM SOURCES keep `llmSourceBindingLabel` untouched, and this is a deliberate exception with a stated cost, not an oversight.** `llmSourceBindingLabel`'s docblock (`ExecuteButton.tsx:100-108`) establishes **egress safety** — *"provider, model, endpoint, and credential bindings stay operator-private"* — which is a genuinely different property from the reader register, and it is a security property this wave must not touch. But `SAFE_LLM_PROFILE_ALIAS = /^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$/` (`:93`) explicitly permits underscores, so a profile alias `finance_default` renders "profile finance_default" — raw snake_case in prose, on a surface this plan classes as prose. Two consequences, both accepted:

- One dialog names models two ways: "model Claude Sonnet 4.6 via openrouter/anthropic" (node) and "profile finance_default" (source). That asymmetry is honest: the node's model IS the destination and is disclosable; the source's provider is deliberately withheld and the alias is all the user is permitted to see. Phrasing the alias would title-case an operator-chosen token into something that looks like a product name.
- **The alias is therefore an IDENTIFIER-surface value inside a prose sentence, and the new whole-dialog pin must be told so.** Pass `allowSelectors: [".run-disclosure-summary li.egress-llm-source"]`… **no** — do not add a class for a test. Instead, **the dialog pin's fixture uses an LLM NODE and not an LLM source**, which is what the existing dialog fixture already does, AND a second explicit test covers the source path with the alias asserted present:

```tsx
  it("shows an LLM source's profile alias verbatim — it is the only disclosable binding (elspeth-59631ec7f7)", () => {
    const lines = buildRunEgressSummary(
      makeComposition({
        sources: { ask_model: { plugin: "llm", options: { profile: "finance_default" }, on_success: "results" } },
        nodes: [],
        outputs: [{ name: "results", plugin: "csv", options: {} }],
      }),
      null, false, [{ name: "llm", plugin_type: "source" } as PluginSummary], false,
    );
    // The alias stays raw BY DESIGN: llmSourceBindingLabel keeps provider,
    // model, endpoint and credential bindings operator-private, and the alias
    // is the one binding the user may see. Phrasing it would invent a name.
    expect(lines.map((line) => line.text).join(" ")).toContain("finance_default");
  });
```

  Adjust the `catalogSources` argument to whatever `catalogFlagsLlmSource` actually requires — read `ExecuteButton.tsx:190-199` and the existing LLM-source tests in this file rather than trusting the shape above. Cost if wrong: a snake_case alias sits in one consent sentence, ruled and tested rather than latent, and the whole-dialog pin would go red on any FUTURE fixture that adds an LLM source with an underscored alias — at which point this ruling is the place to look.

Then:

```ts
export function buildRunEgressSummary(
  compositionState: CompositionState | null,
  catalogTransforms: readonly PluginSummary[] | null = null,
  catalogLoadFailed = false,
  catalogSources: readonly PluginSummary[] | null = null,
  catalogIsLoading = false,
): RunEgressLine[] {
  const reader = egressSentences("reader", compositionState, catalogTransforms, catalogLoadFailed, catalogSources, catalogIsLoading);
  const identifiers = egressSentences("identifier", compositionState, catalogTransforms, catalogLoadFailed, catalogSources, catalogIsLoading);
  return reader.map((text, i) => ({ text, identifiers: identifiers[i] }));
}
```

(Both passes walk the same composition in the same order, so the arrays align by construction — `register` only substitutes label TEXT, it never gates whether a sentence is emitted. That is a property of the code as written and could silently stop holding, so make it checkable rather than asserted in prose: add `if (reader.length !== identifiers.length) throw new Error("egress registers disagreed on line count");` before the `map`. A thrown error on a run-confirm dialog is a loud, fixable failure; a silently misaligned `title` on an audit-required egress line is not.) Imports: `pluginDisplayName`, `titleCaseLabel` from `@/components/catalog/pluginDisplayName`; `stepLabelForNodeId` from `@/components/chat/interpretationStepLabel`; `modelDisplayName` from `@/components/chat/modelDisplayName`. The docblock (`:146-179`) gains one line: "Returns reader-register lines with the identifier-register sentence beside each, surfaced via `aria-describedby` (never `title` alone — see below)."

**Ruling — the identifier sentence reaches the user through `.sr-only` + `aria-describedby`, NOT through `title`. This file already documents why, and already implements the remedy ten lines away.** `ExecuteButton.tsx:668-676`:

> The `title` attribute on the button alone is not reliably announced by all screen readers (NVDA reads it; VoiceOver and some JAWS configurations ignore it). `aria-describedby` pointing at a hidden span is the WCAG-canonical way to surface a "why is this button disabled?" reason

and the fix is implemented at `:677-681` (`<span id={describedById} className="sr-only">`). A pre-fix draft of this step put the identifier sentence in `title` on an `<li>` — adopting the pattern this component's own comment rejects, on an element **worse** than the button the comment was written about: an `<li>` is not focusable, so a keyboard user has no hover and no focus tooltip, and the identifier sentence is unreachable for them by any means at all.

Why the R2-F7 acceptance claim does not cover this: "every `buildRunEgressSummary` call site keeps its identifier array unchanged" proves the function still **produces** the identifier sentences. It does not prove they **reach the user**, and R2-F7 was an under-disclosure defect on a consent surface. A check's scope must match its claim.

Render (`:694-700`) becomes:

```tsx
          {egressLines.length > 0 && (
            <ul className="run-disclosure-summary">
              {egressLines.map((line, index) => {
                const identifiersId = `run-egress-ids-${index}`;
                return (
                  <li
                    key={line.identifiers}
                    aria-describedby={identifiersId}
                    title={line.identifiers}
                  >
                    {line.text}
                    {/* The identifier register, reachable by AT and by
                        keyboard users. `title` is kept for sighted mouse
                        hover; it is a convenience, never the only route
                        (ExecuteButton.tsx:668-676 records why). */}
                    <span id={identifiersId} className="sr-only">
                      {line.identifiers}
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
```

**Second half, recorded and deliberately NOT fixed here.** `components/common/ConfirmDialog.tsx:72` sets `aria-describedby={messageId}` pointing at the `<p className="confirm-dialog-message">` only (`:81-83`); the egress `<ul>` arrives as `children` (`:84`), outside the description. So on dialog OPEN, AT announces "Run pipeline" plus "This run leaves the composer and uses your stored credentials:" and nothing about what leaves — the user must navigate into the list. The `.sr-only` spans above fix the *register* problem (the identifier sentence is now reachable once you are in the list) but not the *announcement-on-open* problem. **Ruling: park the `ConfirmDialog` half with a ticket, do not fix it in this lane.** `ConfirmDialog` is a shared primitive with consumers outside this wave, extending its `aria-describedby` to span message-plus-children is a change to all of them, and this task's Files block does not include it — widening it here is the scope creep the delivery posture warns against. **Task 11 Step 5 files the ticket** (see the Roadmap). Cost if wrong: the run-confirm dialog's egress list is reachable but not announced on open, which is the status quo this step does not worsen.

Tests — `ExecuteButton.test.tsx`: every `expect(buildRunEgressSummary(...)).toEqual([...])` / `const lines = buildRunEgressSummary(...)` site (16) maps to `.map((line) => line.identifiers)` and keeps its string array UNCHANGED — that unchanged array is the R2-F7 proof. Add one reader-register pin next to `:1395-1412`:

```ts
  it("renders reader-register text beside the identifier sentence (elspeth-d74ab492dd)", () => {
    const lines = buildRunEgressSummary(
      makeComposition({
        sources: { source: { plugin: "csv", options: {}, on_success: "classify" } },
        nodes: [{ id: "fetch_page", node_type: "transform", plugin: "web_scrape", input: "source", on_success: "results", on_error: null, options: {} }],
        outputs: [{ name: "results", plugin: "csv", options: {} }],
      }),
    );
    expect(lines.map((line) => line.text)).toEqual([
      "Reads source data: Source (CSV).",
      "Fetches over the network: Fetch Page (Web Scrape).",
      "Writes output: Results (CSV).",
    ]);
  });
```

Dialog pins `:356-360` (the `getByRole("alertdialog", { name: "Run pipeline" })` that scopes them is at `:355`): `"source (csv)"` → `"Source (CSV)"`; the LLM line → `"Classify (model Claude Sonnet 4.6 via openrouter/anthropic)"` **if that fixture's model id carries a provider path — read the fixture and pin what it actually produces**; `"results (csv)"` → `"Results (CSV)"`. Then pin the identifier register at its NEW home, not in `title` alone:

```tsx
    const line = within(dialog).getByText("Reads source data: Source (CSV).");
    // Identifier register reachable by AT and keyboard, per the sr-only ruling.
    const describedBy = line.getAttribute("aria-describedby");
    expect(describedBy).not.toBeNull();
    expect(document.getElementById(describedBy as string)).toHaveTextContent(
      "Reads source data: source (csv).",
    );
    // title kept for mouse hover — a convenience, not the only route.
    expect(line).toHaveAttribute("title", "Reads source data: source (csv).");
```

Add a default-DOM pin on the open dialog: `expectNoIdentifiersInDefaultDom(screen.getByRole("alertdialog", { name: "Run pipeline" }))`.

**That pin now needs the `.sr-only` spans exempted**, because they carry the identifier register on purpose — exactly the situation `allowSelectors` exists for: `expectNoIdentifiersInDefaultDom(dialog, { allowSelectors: [".run-disclosure-summary .sr-only"] })`. Scoped to the egress list, not to `.sr-only` globally: an unscoped `.sr-only` exemption would silently excuse every visually-hidden string in any future dialog. **This is the trade the `.sr-only` ruling buys and it is worth naming: the identifier sentence moves from an attribute the pin never inspected (`title`) into the DOM, where the pin WOULD have caught it, so the exemption is now doing real work and must stay narrow.**

(`makeComposition` here is the file's own helper at `ExecuteButton.test.tsx:99-113`, NOT an import from `@/test/composerFixtures` — verified; a pre-fix draft cited `:31-113`, a range that mostly covers unrelated fixtures such as `READY_READINESS`, and a later draft cited `:99-111`, which clips the helper's last two lines.)

**Add the guard test the throw deserves** — an untested throw is an untested branch, and this one could be placed after the `map` (too late), compare the wrong arrays, or be dropped in review with every suite still green:

```ts
  it("keeps the two egress registers aligned line-for-line across every branch", () => {
    // The `reader.length !== identifiers.length` throw guards an alignment
    // that holds "by construction" — register substitutes label TEXT and
    // never gates whether a sentence is emitted. This exercises the property
    // over the file's branchy fixtures rather than forcing the throw.
    for (const args of EGRESS_FIXTURE_CASES) {
      const lines = buildRunEgressSummary(...args);
      // `every` returns true on an empty array, so a tuple that yields no
      // lines would pass vacuously. Guard the guard.
      expect(lines.length).toBeGreaterThan(0);
      // THE assertion: a short `identifiers` array yields `undefined` at the
      // tail, which is exactly what misalignment looks like.
      expect(lines.every((line) => typeof line.identifiers === "string")).toBe(true);
    }
    // NOT `expect(lines.map(l => l.text)).toHaveLength(lines.map(l => l.identifiers).length)`
    // — a first fix round wrote that, and both sides are `map` over the SAME
    // array, so their lengths are equal by definition of `map` no matter how
    // misaligned the registers are. A tautology introduced while fixing a
    // missing-test finding.
  });
```

where `EGRESS_FIXTURE_CASES` is an array assembled from the argument tuples the 16 existing call sites already use — **especially the branchy ones with no reader coverage at all**: `catalogLoadFailed`, `catalogIsLoading`, the LLM-source path, and the four network/unverifiable sites. Those have identifier coverage and no reader coverage, and this is the cheapest way to give the invariant that binds them one assertion. Do not hand-write new fixtures; lift the tuples.

Run: `npx vitest run src/components/sidebar` → PASS. Stage from the repo root — only this step's two files:

```bash
git add src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.tsx src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.test.tsx
git status --short   # nothing else staged; the .sr-only class already exists (no stylesheet change)
```

Commit: `"feat(run-confirm): egress lines in the reader register, identifier sentence reachable via sr-only + aria-describedby; model keeps its provider path — every sentence kept (elspeth-d74ab492dd, elspeth-59631ec7f7)"`. **The message must not say "in title":** this is the commit that FIXES the `title`-only defect, and a message saying otherwise misrecords it permanently in git history.

- [ ] **Step 7 (provenance enum): label map**

`InlineSourceCreatedTurn.tsx` — near `EDITABLE_PROVENANCES` (`:50-53`):

```ts
/** Reader-register labels for the closed provenance enum; the raw value
 *  stays in `title` (elspeth-d74ab492dd). Exhaustive by type: adding a
 *  provenance without a label is a compile error. */
export const INLINE_SOURCE_PROVENANCE_LABELS: Record<InlineSourceProvenance, string> = {
  verbatim: "Typed by you",
  "llm-generated": "Drafted by the composer",
  disambiguated: "Chosen by you from the options offered",
  "llm-generated-then-amended": "Drafted by the composer, then edited by you",
};
```

`:190-193`: `<dd>{summary.provenance}</dd>` → `<dd title={summary.provenance}>{INLINE_SOURCE_PROVENANCE_LABELS[summary.provenance]}</dd>`. Test (the audit `<details>` opens via its `<summary>` "Show audit info", `:174`):

```tsx
  it("labels provenance in the reader register with the raw enum in title (elspeth-d74ab492dd)", () => {
    render(<InlineSourceCreatedTurn summary={{ ...llmGenerated, provenance: "llm-generated-then-amended" }} onEdit={vi.fn()} />);
    fireEvent.click(screen.getByText("Show audit info"));
    const dd = screen.getByText("Drafted by the composer, then edited by you");
    expect(dd).toHaveAttribute("title", "llm-generated-then-amended");
  });
```

Run: `npx vitest run src/components/chat/InlineSourceCreatedTurn.test.tsx` → PASS. Commit:

```bash
git add src/elspeth/web/frontend/src/components/chat/InlineSourceCreatedTurn.tsx src/elspeth/web/frontend/src/components/chat/InlineSourceCreatedTurn.test.tsx
git commit -m "feat(chat): inline-source provenance reads as prose, raw enum in title (elspeth-d74ab492dd)"
```

- [ ] **Step 8 (component review "reviewed"): drop the word**

`ComponentReviewTurn.tsx:76-84`:

```tsx
            <li
              key={item.stable_id}
              className="guided-component-review-item"
              aria-label={`${item.name}, ${pluginDisplayName(item.plugin)}`}
            >
              <div className="guided-component-review-summary">
                <strong>{item.name}</strong>
                <span title={item.plugin}>{pluginDisplayName(item.plugin)}</span>
              </div>
```

(`item.status` is closed to `"reviewed"` — `types/guided.ts:557` — so the word carried no information; no glyph, the list heading "Review sources" already says what the rows are. Import `pluginDisplayName`.) Tests `:43-44,:89-90,:100-101`: `"customers, csv, reviewed"` → `"customers, CSV"`, `"orders, json, reviewed"` → `"orders, JSON"`. `item.name` is a guided identifier surface (the user just typed it; "Edit customers"/"Remove customers" buttons name it) — no default-DOM pin on this turn; recorded under the one-rule constraint.

Run: `npx vitest run src/components/chat/guided/ComponentReviewTurn.test.tsx` → PASS. Commit:

```bash
git add src/elspeth/web/frontend/src/components/chat/guided/ComponentReviewTurn.tsx src/elspeth/web/frontend/src/components/chat/guided/ComponentReviewTurn.test.tsx
git commit -m "feat(guided): component-review rows drop the literal 'reviewed', plugin by display name (elspeth-d74ab492dd)"
```

- [ ] **Step 9: Whole-directory run + typecheck**

`npx vitest run src/components/chat src/components/settings src/components/execution src/components/audit src/components/sidebar src/components/catalog src/styles` → PASS; `npx tsc --noEmit -p tsconfig.app.json && npx tsc --noEmit -p tsconfig.test.json` → clean; `npm run lint` → clean.

Ticket disposition at Task 11: close `elspeth-d74ab492dd` (comment lists the eight items + the Spec-tab `<h4>`/kind/policy items landed in Task 4's commit) and `elspeth-59631ec7f7` (comment states the one rule and the two commits that apply it).

---

### Task 6: Freeform brief — reply in the reader's terms (`elspeth-4bf65fe149`)

**Files:**
- Modify: `src/elspeth/web/composer/skills/pipeline_composer.md` (insert a section before `## Requested Workflow Integrity`, `:134`; add a checklist line in `## Termination States`, `:851-860`)
- Modify: `tests/unit/web/composer/test_prompts.py` (brief-content pin)

**Interfaces:**
- Consumes: `SYSTEM_PROMPT` (`prompts.py:65` — `SYSTEM_PROMPT = render_with_pipeline_capabilities(_strip_advisor_disabled_fallback(_PIPELINE_SKILL))`, where `_PIPELINE_SKILL` = `pipeline_composer.md` via `load_skill_with_hash`; `test_prompts.py:35` already imports it). `_strip_advisor_disabled_fallback` (`:56-62`) removes only `<!-- ADVISOR-DISABLED -->…<!-- /ADVISOR-DISABLED -->` blocks, and the insertion point at `:134` is outside any such block, so all three assertions below reach live text.
- **`SYSTEM_PROMPT` also reaches the GUIDED lane, at exactly one seam, and that seam is the only route by which this change touches the tutorial.** The guided lane builds its prompts from per-step skills (`guided/prompts.py:91-94`, `load_step_planner_skill(step)` → `load_step_chat_skill(step)`) and does **not** load `pipeline_composer.md` — so the nine guided steps are byte-unaffected. The one exception is the guided→freeform **graduation**: `guided/prompts.py:97` `build_mode_transition_system_prompt(*, terminal_reason, freeform_skill)` takes the fully processed freeform skill, whose docstring (`:100-104`) says it is supplied *"typically via `build_system_prompt(data_dir)` in `composer/prompts.py`"* — i.e. the whole freeform brief including this task's new `## Reply Register` section and the new Termination States checklist line. **This is not a tutorial-special path (ADR-031 holds — nothing branches on tutorial-ness); it is a change to the register of the model's replies after graduation, which is the tutorial's final user-visible step.** Task 11 Step 4 item 9's canary criterion is written against exactly this seam.
- The skill hash (`composer_skill_hash` audit column, `web/composer/skills/__init__.py:40`; the decorated definition spans `:39-62` — `:39` is `@lru_cache(maxsize=8)`, a DECORATOR not documentation, `:40` the `def`, docstring `:41-58`, body `:59-62`) is computed at load time, not pinned in the tree. Verified by `grep -rnE '\b[0-9a-f]{64}\b' tests/unit/web/composer/`: **zero** hits in `test_prompt_cache_layout.py` and `test_capability_skill_identity.py`, and `test_compose_loop_interpretation_review_dispatch.py:2869-2899` derives the pristine hash dynamically. That grep is not empty overall — it also returns `test_state_serialisation_contract.py:159-174` (composition-state hashes, which a skill-text change cannot move) and `redaction_policy_snapshot.json` — **so a reader who re-runs it and sees hits has not found a defect; those two are named here so the search does not have to be repeated.** Changing the text changes the skill hash for every subsequent audit row: that is the honest record of a brief change, not churn.
- **No size ceiling is at risk.** `test_prompt_cache_layout.py`'s only budget is a MARKER-count assertion (`test_marker_budget_is_at_most_four_with_tools`, `:133-145`), not a byte or token budget, so ~20 added lines cannot trip it. The one content pin on the skill's structure is `test_prompts.py:788` (`assert "## Requested Workflow Integrity" in result`); this task inserts BEFORE that heading, so it survives.

**Ruling — the pin lives in `test_prompts.py`, not `test_pipeline_planner.py`:** the ticket names the planner suite, but the reply it observed is the FREEFORM compose loop's, whose brief is `SYSTEM_PROMPT` (owned by `prompts.py`); `test_pipeline_planner.py` covers the guided planner (`pipeline_planner.py`) and has no brief-content tests (grep `brief` → 0 hits; its one mention of `pipeline_composer.md` at `:9119` is a docstring). The existing skill-content pins are `test_tool_declarations.py:730` (reads the file) and `test_prompts.py` (asserts on `SYSTEM_PROMPT`). Cost if wrong: a test in the module that owns the prompt rather than the one the ticket guessed — nil.

**Ruling — brief-text pin only, no reply fixture:** `elspeth-4bf65fe149` names the fallback itself — "or, if none, a unit test on the brief text only — do not invent a corpus harness for one case" — and there is no reply-quality assertion in any composer suite to attach a fixture to. That clause is the whole reason, and it is sufficient. The roadmap row's "corpus case that fails on `is_valid:`/`options.` tokens" is therefore SATISFIED by the ticket's own fallback, not left as a scope gap. (A pre-fix draft also cited Composer invariant 1 here. That was wrong and is struck: invariant 1 governs the runtime AUTHORING path — no server-side synthesis, routing or filtering of pipeline structure reaching the user — and a pytest assertion over a recorded reply is not on that path. Citing it to decline a test would license refusing legitimate tests later.) The pin asserts the instruction is present and phrased as the user register; live behaviour is checked in Task 11 Check 3.

- [ ] **Step 1: Write the failing pin**

Append to `tests/unit/web/composer/test_prompts.py`:

```python
class TestReplyRegisterRule:
    """The freeform brief tells the model to summarise in the reader's terms
    (elspeth-4bf65fe149). Observed live (session 39578c6f): the final reply
    echoed ``is_valid: true``, ``options.profile`` and ``require_all/union``.
    That is a brief defect — the fix is instruction, never server-side
    rewriting (Composer invariant 1) and never a tutorial branch (ADR-031)."""

    def test_brief_carries_the_reply_register_section(self) -> None:
        assert "## Reply Register" in SYSTEM_PROMPT

    def test_brief_names_the_three_identifier_classes_it_forbids_in_prose(self) -> None:
        section = SYSTEM_PROMPT.split("## Reply Register", 1)[1].split("\n## ", 1)[0]
        for phrase in (
            "tool-argument keys",
            "validation payload fields",
            "enum values",
            "Spec and YAML tabs",
            "display label",
            # The failure half. A rule that supplies a replacement phrase for
            # the SUCCESS case only ("validation passed", not is_valid: true)
            # leaves the model no instructed form for a failure, and the
            # nearest compliant behaviour is vagueness. Pin the clause that
            # forbids that, not just the ones that forbid the tokens.
            "whether validation passed or failed",
        ):
            assert phrase in section, phrase

    def test_termination_checklist_includes_the_register_line(self) -> None:
        checklist = SYSTEM_PROMPT.split("## Termination States", 1)[1]
        assert "no tool-argument keys, validation fields, or enum values in prose" in checklist
```

Run: `source .venv/bin/activate && pytest tests/unit/web/composer/test_prompts.py -k ReplyRegister -q` → 3 FAIL.

**On what these three tests are and are not:** they are declaration tests over text this plan itself authors — they prove the section is present and phrased as intended, not that the model obeys it. The ticket's own fallback authorises exactly that ("or, if none, a unit test on the brief text only"), and live behaviour is observed at Task 11 Check 3, which is explicitly **not** a pass/fail gate. So after this wave there is no blocking signal on the rule's effect. That trade is acceptable **only because the instruction itself cannot license under-reporting** — which is what the failure-case clause above and its pin are for. Do not drop either as redundant.

- [ ] **Step 2: Add the section and the checklist line**

Insert immediately before `## Requested Workflow Integrity` (`pipeline_composer.md:134`, i.e. after the "Step Descriptions" paragraph):

```markdown
## Reply Register

Your replies are read by the person who asked for the pipeline, on a narrow
chat rail beside the Spec and YAML tabs. Those tabs are where identifiers
live; your prose is where meaning lives.

- Summarise what the pipeline does in the user's own terms: what comes in,
  what each step does to it, what goes out, and which decisions you made and
  why. Keep the "why I did X" rationale — it is the legibility layer.
- Refer to every step by its display label (its description, or its name in
  Title Case), never by node id, plugin id, or connection name.
- Do not echo tool-argument keys (`options.profile`, `prompt_template_parts`),
  validation payload fields (`is_valid: true`, `errors: []`), or enum values
  (`require_all`, `union`, `passthrough`) in prose. Say "waits for every
  branch", not "policy: require_all". Say "validation passed", not
  "`is_valid: true`".
- Always say plainly whether validation passed or failed, and if it failed,
  what failed and where, in the reader's terms — name the step by its display
  label and describe the problem as a sentence. This rule governs *how* you
  name things, never *whether* you report an outcome. "There were some issues"
  is a worse reply than "`is_valid: false`", not a better one.
- Do not paste an ASCII topology tree or a YAML excerpt into the reply; the
  Graph and YAML tabs render those exactly.
- If the user asks for the identifiers, give them — this rule governs
  unprompted summaries, not direct questions.
```

In `## Termination States`, add a checklist line after the existing "My prose uses the user register" line (`:860`):

```markdown
- [ ] My summary is in the reader's terms — steps by display label, no tool-argument keys, validation fields, or enum values in prose (Reply Register).
```

Run: `pytest tests/unit/web/composer/test_prompts.py tests/unit/web/composer/test_tool_declarations.py tests/unit/web/composer/test_prompt_cache_layout.py tests/unit/web/composer/test_capability_skill_identity.py tests/unit/web/composer/test_skills_loader.py tests/unit/web/composer/test_compose_loop_interpretation_review_dispatch.py -q` → PASS (the last one derives hashes dynamically; a failure there means a pinned hash exists that this plan did not find — stop and report, do not re-pin).

- [ ] **Step 3: Per-task lint-corpus diff (backend-touching)**

```bash
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  elspeth-lints check --rules all --root src/elspeth > "$W3_CORPUS_DIR/after-task6.txt"
diff "$W3_CORPUS_DIR/before.txt" "$W3_CORPUS_DIR/after-task6.txt"
```

Expected: empty (a Markdown file and a `tests/` edit cannot move a `src/elspeth` corpus; the run is the proof). `$W3_CORPUS_DIR` is set by Task 0 Step 1 — export it in your shell from the path Task 0 recorded. **The after-file is named for THIS task**: a pre-fix draft wrote `/tmp/w3-lints-task5.txt` from inside Task 6, which is harmless only while Task 5 runs no corpus check and is a silent cross-lane overwrite the moment one is added.

- [ ] **Step 4: Commit**

```bash
git add src/elspeth/web/composer/skills/pipeline_composer.md tests/unit/web/composer/test_prompts.py
git commit -m "feat(composer-brief): Reply Register section — summarise in the reader's terms, identifiers stay on the Spec/YAML tabs (elspeth-4bf65fe149)"
```

---

### Task 7: e2e — keyboard path through the graph a11y component list (`elspeth-d1feee1e67`)

**Files:**
- Create: `src/elspeth/web/frontend/tests/e2e/composer-workspace-graph-keyboard.spec.ts` (the name matches the `tests/e2e/composer-workspace-*.spec.ts` glob in `package.json:17`, so `npm run lint` covers it with no script change)

**Interfaces:**
- Consumes: `installWorkspaceScenario` / `deleteWorkspaceScenario` (`helpers/workspace-fixtures.ts:538,:594`), `setupWorkspaceScenario` (`helpers/workspace-setup.ts:1`), `ComposerPage` (`page-objects/composer-page.ts` — `goto`, `waitForChatReady`, `artifactTab("Graph")`, `chatInput()`).
- The scenario `populated-long-transcript` seeds a composition with one source (`source`, csv) and one output (`results`, csv) and NO nodes (`seedCanonicalComposition`, `workspace-fixtures.ts:109-190`; the node array is populated only for `tall-confirmation-dialog`, `:120-131`) — two entries in the a11y list, which is enough to Tab between items. It does NOT use `tall-confirmation-dialog`, so `elspeth-71bbf7eb12` (the tall-dialog `/validate` timeout) cannot affect this spec.
- The list: `<ol className="graph-a11y-list" aria-label="Pipeline components in source-to-sink order (N)">` (`GraphView.tsx:1888-1890`), one `<button>` per component; activation calls `selectNode` and sets `focusPanelOnOpenRef` so the config panel (the `<aside>` opens at `GraphView.tsx:684`, with `ref` `:685`, `tabIndex={-1}` `:686`, `role="complementary"` `:691` and `aria-label={`${config.id} configuration`}` `:692`) receives focus (`focusPanelOnOpenRef` `:810`, effect `:811-818`). CSS: `.graph-a11y-list` is a 1px clip, `.graph-a11y-list:focus-within` reveals it (clip `inspector.css:337-349`, reveal `:351-365`).

**Coverage gap, stated rather than silently accepted (no evidence of a current violation either way):** `inspector.css:333-336` justifies the reveal as giving "a sighted keyboard user a VISIBLE focus target (WCAG 2.4.7)". This spec asserts the LIST's box grows; it asserts nothing about the focused `<button>` having a focus indicator, and nothing about 2.4.11 Focus Not Obscured (AA, new in WCAG 2.2 — the list is `position: absolute` with `z-index: var(--z-panel-controls)`, so a higher-stacking overlay would obscure it). Neither is claimed by this spec and neither should be claimed in the ticket closeout. If the lane wants the stronger guard, a computed-style assertion on `outline`/`box-shadow` of the focused item is the cheap instrument; a masked screenshot comparison is the thorough one. **Do not write the closeout as "keyboard access is pinned" — write it as "the list's reveal-on-focus and the focus hand-off to the config panel are pinned".**

**Ruling — Tab/Shift+Tab move between items; ArrowDown is NOT implemented and this spec does not assert it.** The list is plain `<button>`s with no roving tabindex; the ticket's "ArrowDown/Enter" assumed one. Adding arrow-key navigation is a feature (a parity sweep across the a11y suite), not an e2e pin — parked in the Roadmap. Cost if wrong: the spec passes today and would still pass after arrow keys land.

- [ ] **Step 1: Write the spec**

```ts
import { expect, test, type Locator, type Page } from "@playwright/test";

import {
  deleteWorkspaceScenario,
  installWorkspaceScenario,
} from "./helpers/workspace-fixtures";
import { ComposerPage } from "./page-objects/composer-page";
import { setupWorkspaceScenario } from "./helpers/workspace-setup";

// The graph's accessible component list is a 1px clip that reveals on
// :focus-within (inspector.css .graph-a11y-list) — a deliberate keyboard-only
// disclosure (elspeth-57c6fba409 closed not_a_bug). Nothing else pins that
// path end to end: unit tests never measure geometry, so a CSS change that
// dropped the :focus-within block would leave every vitest suite green while
// making the list unreachable for sighted keyboard users (elspeth-d1feee1e67).

const VIEWPORT = { width: 1536, height: 760 };
const MAX_TABS_TO_REACH_LIST = 40;

async function boundingSize(locator: Locator): Promise<{ width: number; height: number }> {
  const box = await locator.boundingBox();
  if (box === null) throw new Error("element has no bounding box");
  return { width: box.width, height: box.height };
}

async function tabUntilFocusWithin(page: Page, container: Locator): Promise<void> {
  for (let i = 0; i < MAX_TABS_TO_REACH_LIST; i += 1) {
    if (await container.evaluate((el) => el.contains(document.activeElement))) return;
    await page.keyboard.press("Tab");
  }
  throw new Error(`focus did not enter the component list within ${MAX_TABS_TO_REACH_LIST} Tab presses`);
}

test.describe("graph a11y component list — keyboard path", () => {
  test("is a 1px clip until focus enters it, then reveals and opens the config panel on Enter", async ({ page }) => {
    await page.setViewportSize(VIEWPORT);
    const { sessionId, value: composer } = await setupWorkspaceScenario(
      () => installWorkspaceScenario(page, "populated-long-transcript"),
      async (createdSessionId) => {
        const created = new ComposerPage(page);
        await created.goto(createdSessionId);
        await created.waitForChatReady();
        return created;
      },
      (createdSessionId) => deleteWorkspaceScenario(page, createdSessionId),
    );
    try {
      await composer.artifactTab("Graph").click();
      const list = page.getByRole("list", { name: /pipeline components in source-to-sink order/i });
      await expect(list).toBeAttached();

      // Pointer users can never hit it: with focus elsewhere the box is ≤1px.
      await composer.chatInput().focus();
      const collapsed = await boundingSize(list);
      expect(collapsed.width).toBeLessThanOrEqual(1);
      expect(collapsed.height).toBeLessThanOrEqual(1);

      // Tab from the Graph tab until focus lands inside the list (the reveal).
      await composer.artifactTab("Graph").focus();
      await tabUntilFocusWithin(page, list);
      const revealed = await boundingSize(list);
      expect(revealed.width).toBeGreaterThan(1);
      expect(revealed.height).toBeGreaterThan(1);
      const items = list.getByRole("button");
      await expect(items).toHaveCount(2); // source + results; no nodes in this scenario
      await expect(items.first()).toBeFocused();

      // Tab moves between items (plain buttons, no roving tabindex); Enter
      // activates and hands focus to the configuration panel.
      await page.keyboard.press("Tab");
      await expect(items.nth(1)).toBeFocused();
      await page.keyboard.press("Enter");
      const panel = page.getByRole("complementary", { name: /configuration$/ });
      await expect(panel).toBeVisible();
      await expect(panel).toBeFocused();
      await expect(panel).toHaveAccessibleName("results configuration");

      // Focus left the list, so it collapses again.
      const collapsedAgain = await boundingSize(list);
      expect(collapsedAgain.width).toBeLessThanOrEqual(1);
      expect(collapsedAgain.height).toBeLessThanOrEqual(1);
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });
});
```

- [ ] **Step 1b: Fold the clip assertion and `expect.poll` INTO the spec before running anything**

**The spec code block above does not contain them.** They arrive as prose in Step 3, 20-50 lines further down, and an implementer who copies the block and moves on lands the weaker spec — geometry-only, blind to `clip`, with no auto-retry. That is not a hypothetical: the block is the copyable artefact and the prose is not. Before Step 2, confirm all three are IN the file:

- [ ] the collapsed-phase `clip` assertion (`expect.poll(() => list.evaluate((el) => getComputedStyle(el).clip)).toBe("rect(0px, 0px, 0px, 0px)")`)
- [ ] the revealed-phase `clip` assertion (`… .toBe("auto")`)
- [ ] `expect.poll` wrapping all three `boundingBox()` reads, not a bare `expect`

If any is missing, the second control run in Step 3 will PASS when it must fail, and the spec ships blind to the half of the collapse that geometry cannot see.

- [ ] **Step 2: Run it (one Playwright process per worktree)**

From `src/elspeth/web/frontend`: `npx playwright test tests/e2e/composer-workspace-graph-keyboard.spec.ts` → PASS. If `items.first()` is not the element focus lands on first (Tab order puts the source button first because sources precede outputs in `accessibleNodes` — the three push loops are `GraphView.tsx:1806-1814`), read the failure and fix the ASSERTION order, not the component. Then `npx tsc --noEmit -p tsconfig.e2e.json` → clean (one pre-existing TS7016 for `scripts/staging-tutorial-driver.mjs` is `elspeth-062c1d0b7f`, not this task) and `npm run lint` → clean.

- [ ] **Step 3: Negative control (the reason this spec exists)**

A spec that has never been seen to fail is not yet a guard. Prove this one catches the regression it exists for — **without touching a tracked file.** The reveal rule is `.graph-a11y-list:focus-within` (`inspector.css:351-365`); neutralise it in the page instead:

```ts
// Run this variant ONCE, by hand, then delete it — it is a control, not a
// committed test. Same body as the spec above, with one line inserted
// immediately after `await composer.artifactTab("Graph").click();`:
await page.addStyleTag({
  content: `.graph-a11y-list:focus-within { width: 1px !important; height: 1px !important; clip: rect(0 0 0 0) !important; }`,
});
```

Expected: the spec now FAILS on `expect(revealed.width).toBeGreaterThan(1)`. Remove the injected line, re-run → PASS.

**Run the control a SECOND time, injecting ONLY the clip override — because the spec as written is blind to `clip`, which is half of what hides the list.** `inspector.css:337-349` collapses with `width: 1px; height: 1px; overflow: hidden; clip: rect(0, 0, 0, 0)`, and `:351-365` reveals with `width: auto; height: auto; … clip: auto`. On a `position: absolute` element, `clip` clips the **painted** region; `getBoundingClientRect()` returns the **layout** box and is unaffected by it, and Playwright's `toBeVisible()` does not consider `clip` either. **So a regression that dropped only `clip: auto` from the `:focus-within` block — leaving `width: auto; height: auto` — would make the list invisible to sighted keyboard users while this spec stayed green.** That is precisely the regression class the spec exists to guard. Because the first control injects width, height and clip together, it cannot distinguish which assertion is load-bearing.

```ts
// Second control run, also once and by hand:
await page.addStyleTag({
  content: `.graph-a11y-list:focus-within { clip: rect(0 0 0 0) !important; }`,
});
```

Expected on the spec AS WRITTEN: **PASS** — which is the finding. So close it in the spec itself rather than leaving the caveat in prose; add one assertion beside the geometry ones:

```ts
    // clip is the other half of the collapse and geometry cannot see it:
    // getBoundingClientRect returns the LAYOUT box, and `clip` affects only
    // the painted region. Without this, dropping `clip: auto` from the
    // :focus-within block hides the list with every assertion still green.
    await expect
      .poll(() => list.evaluate((el) => getComputedStyle(el).clip))
      .toBe("auto");
```

and assert the inverse (`rect(0px, 0px, 0px, 0px)`) in the collapsed phase. With that assertion present, re-run the clip-only control: the spec must now FAIL. **Record all three runs — baseline green, full-override red, clip-only red — in the commit message.** Two Playwright processes must never run concurrently in this worktree (auth state is worktree-global), so these are sequential.

**Use `expect.poll` for the geometry reads too.** `boundingSize()` snapshots a number and asserts with a plain `expect(...)`, which has no auto-retry, and three of the reads follow a state transition — the last immediately after `Enter` moves focus out of the list. The flake window is small (`inspector.css:337-365` has no transition or animation, so the clip/reveal is instantaneous, and the final read follows an auto-retrying `await expect(panel).toBeFocused()`), but `expect.poll(async () => (await list.boundingBox())?.width ?? 0).toBeGreaterThan(1)` costs nothing and removes the class of failure entirely. Same for the two collapse reads.

**Keep the controls inspectable rather than only described.** A later reviewer cannot reproduce a control that exists solely in a commit message. Leave both variants in the spec file as `test.skip(...)` siblings with a comment saying they are controls, run by hand, expected to fail the assertion named in the comment. That costs nothing at run time (skipped tests do not execute) and makes the mechanism readable. **Be honest in the closeout about what is and is not controlled:** the two controls prove the reveal-geometry assertion and the clip assertion are load-bearing. The `toBeFocused()`, `toHaveAccessibleName("results configuration")` and collapse-again assertions have NO control, and the closeout should not imply they do.

**Ruling — inject the override, never edit the stylesheet in place; and state the narrower claim honestly.** This is a shared checkout with another lane's uncommitted work in it, so `git checkout -- inspector.css` is forbidden outright (Global Constraints), and a `cp` backup/restore round-trip is the same hazard in a narrower window: for the duration of the run the tracked file holds content nobody staged, and a sibling's `git add -A` or a failed restore keeps it. What `addStyleTag` proves is slightly weaker than editing the source: it shows the assertion is load-bearing on the list's REVEALED GEOMETRY — kill the reveal and the spec goes red — rather than that this particular CSS block is the only thing producing it. That is the property the spec is for, and it is worth the honesty. The stronger control (edit `inspector.css` inside a throwaway `git worktree`, run there, delete the worktree) is available if a reviewer wants the source-level version; it costs a worktree and a second Playwright run, and **must not overlap the first** — Playwright auth state is worktree-global and two concurrent runs corrupt it.

**A port override does NOT make concurrent runs safe, and a lane will try it.** Both ports are env-overridable (`PLAYWRIGHT_BACKEND_PORT` / `PLAYWRIGHT_FRONTEND_PORT`, `playwright.config.ts:6-19`, defaults 8451/5173), but `STORAGE_STATE_PATH` (`:35`) is anchored to the config directory (`HERE`), **not** to `E2E_RUN_ID` (which only namespaces `.e2e-data`). So overriding the ports removes the clean port-collision error and leaves the shared auth file colliding instead — trading a loud failure for a flaky one. Serialise; do not parallelise with a port flag. **Task 11 Step 1b also runs Playwright: confirm this lane has landed and no Playwright run is in flight before starting it.** Cost if wrong: the control passes while some other rule also collapses the list, and the spec is a slightly weaker guard than advertised — recorded here rather than discovered later.

- [ ] **Step 4: Commit**

```bash
git add src/elspeth/web/frontend/tests/e2e/composer-workspace-graph-keyboard.spec.ts
git commit -m "test(e2e): keyboard path through the graph a11y component list — 1px clip, :focus-within reveal, Enter focuses the config panel; negative control run (elspeth-d1feee1e67)"
```

---

### Task 8: Minor `show_advanced` gates (`elspeth-f1394307e3`)

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/recovery/RecoveryPanel.tsx` (`:1-6` imports, `:46` state, **transcript block `:158-176`** — `:177` is the `</div>` closing the ENCLOSING container, so wrapping through `:178` breaks the JSX), `RecoveryPanel.test.tsx` (`:92-114`)
- Modify: `src/elspeth/web/frontend/src/components/blobs/BlobRow.tsx` (`:145-147` summary LINE derivation, `:271-273` count paragraph — **not** the `:141-144` memo and **not** the whole `:263-275` block; see the caveat ruling), `BlobRow.test.tsx` (`:98,:125,:182,:208` structure pins **and `:214`**, the fifth test that depends on the same block)
- Modify: `src/elspeth/web/frontend/src/components/audit/AuditReadinessPanel.tsx` (Refresh — the `<Button` tag **opens at `:520`** and closes at `:542`; both `:521` and `:522` land inside the tag, and the ticket body carries the same drift), `AuditReadinessPanel.test.tsx` (`:396`, `:494`)

**Interfaces:**
- Consumes: `useShowAdvanced()`, `resetStore(usePreferencesStore)`, `expectNoIdentifiersInDefaultDom`.
- "Show archived" (HeaderSessionSwitcher) is NOT in scope — it is the only archive-restore path (ticket text).

**Ruling — omit, don't hint:** with the flag off each surface loses only the technical control; its sibling content is the plain summary (RecoveryDiff + Discard/Apply; the blob preview; the audit panel, which already refetches on every composition version, `AuditReadinessPanel.tsx:329-353`). No "turn on Detail level to see…" copy — the Wave 2 `CompletionBar` precedent (`CompletionBar.tsx:103`, Import YAML simply absent). Cost if wrong: a user who wants the transcript must know the preference exists — it is documented on the preferences panel.

**Ruling — the BlobRow truncation CAVEAT stays visible at every detail level; only the row/column COUNTS are gated.** A pre-fix draft gated the `useMemo` at `BlobRow.tsx:141-144` so it returned `null` early, which removes the whole `.blob-row-structure` block — **including the caveat `<p>` at `:268-270`.** `BlobRow.tsx:137-140` describes that caveat as the honesty mechanism: *"a truncated/ragged/oversized/unparseable body surfaces a plain caveat instead of a guessed row count."* Gating it violates this wave's own Global Constraint that every item hidden with the flag off has a plain summary in its place — the blob preview is named as that summary, and the preview does not carry the caveat.

**Scope it on the RIGHT cases — a first fix round justified this on truncation and that justification is false.** That round argued truncated content would render with "no signal at all" and that "a screen reader reading a clipped `<pre>` receives nothing (WCAG 1.3.1)". `BlobRow.tsx:284-291` contradicts both:

```tsx
<pre className="blob-row-preview-pre">
  {displayContent}
  {truncated && (
    <span className="blob-row-preview-truncated">
      {"\n... (truncated)"}
    </span>
  )}
</pre>
```

That is a real text node inside that very `<pre>`, in the accessibility tree; a screen reader does receive it. **Truncation is the ONE caveat case the `<pre>` already covers, and the WCAG 1.3.1 cite does not apply.** Committing an invented mechanism as a justification is what this plan corrects in Task 10; it must not do it here.

**The conclusion stands on the cases the `<pre>` does NOT cover** — the "structure couldn't be read / couldn't be confirmed" family that `summarizeContentStructure` emits with no counterpart anywhere else in the row: an unterminated quoted field (`utils/contentStructure.ts:168`, *"structure couldn't be read"*), a ragged CSV (`:198`, *"Rows don't all have the same number of columns — structure couldn't be read"*), a blank line in JSONL (`:322`), a line that is not valid JSON (`:333`), "content is empty — nothing to summarise" (`:145`, `:155`, `:221`, `:293`), and the two *"truncated before a complete row/line was captured — structure couldn't be confirmed"* cases (`:181`, `:306`), which are ABOUT truncation but say something the `<pre>`'s marker does not: that the structure could not be read at all. (The plain *"Preview is truncated — full row count couldn't be confirmed"* at `:208` is the one caveat the `<pre>`'s marker duplicates, so it is NOT part of this ruling's basis.)

**The test below nonetheless uses a TRUNCATED fixture, and the two facts are not in tension — keep them apart.** The ruling's BASIS is the non-truncation caveats, which nothing else in the row supplies. The test's FIXTURE is forced by a separate, mechanical constraint: `describeStructuralSummary` (`:398-408`) returns `null` unless `rowCount` or a non-empty `fields` survives, and the ragged branch (`:193-200`) sets **both to null** — so a ragged fixture produces no summary line at any detail level and a "the counts are gated" assertion **cannot fail**. Only the truncated branch (`:203-209`) yields a caveat AND a real `columns: …` line, which is what makes the gate observable. The well-formed branch (`:212`) is the only one with a `rowCount`, and it has `caveat: null` — **so no single fixture carries a caveat AND a row count, and the two halves of the gate need two tests.** Gate those away and a body the engine could not parse reads as a clean preview, at the default detail level, with nothing anywhere saying otherwise. That is the content-integrity violation of the plan's own constraint; it is not primarily a WCAG finding and should not be argued as one.

The structural *introspection* — "3 rows; columns: name, age" — is the engineer-register part and is correctly gated. The caveat is a data-honesty disclosure and is not. Cost if wrong: a user at the default detail level reads an unparseable or ragged body as a clean one.

- [ ] **Step 1: Failing tests**

`RecoveryPanel.test.tsx` — add imports `import { beforeEach } from "vitest";` (extend the existing vitest import at `:4`), `import { usePreferencesStore } from "@/stores/preferencesStore";`, `import { resetStore } from "@/test/store-helpers";`, `import { expectNoIdentifiersInDefaultDom } from "@/test/defaultDomPins";`; add a top-level `beforeEach(() => resetStore(usePreferencesStore));`. In the test at `:92` ("renders headline reason evidence diff transcript and controls") set `usePreferencesStore.setState({ showAdvanced: true });` as its first line (it asserts the transcript + button). Add:

```tsx
  it("omits the raw transcript and its controls with the flag off; keeps the diff and both actions (elspeth-f1394307e3)", () => {
    const { container } = renderPanel();
    expect(screen.getByText("Pipeline changes")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply partial draft" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Discard recovery" })).toBeInTheDocument();
    expect(screen.queryByText("Tool transcript")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View raw transcript controls" })).not.toBeInTheDocument();
    expectNoIdentifiersInDefaultDom(container);
  });
```

`BlobRow.test.tsx` — same three imports + top-level `beforeEach(() => resetStore(usePreferencesStore));`; the four tests that assert on `findByTestId("blob-row-structure")` (`:98,:125,:182,:208`) get `usePreferencesStore.setState({ showAdvanced: true });` as their first line, **and so does the fifth test at `:214`.**

**`:214` is easy to miss and goes silently vacuous without it.** That test is `"does not render a structure block for content types with no structural handling (e.g. text/plain)"`, and its assertion at `:237` is `expect(screen.queryByTestId("blob-row-structure")).not.toBeInTheDocument()`. Under the caveat ruling above the block is no longer wholly flag-gated, so this test still discriminates on mime type — **but only if the fixture can produce a block at all.** Setting the flag makes the discrimination it was written for (`format === "unsupported"` for `text/plain`) the thing under test, rather than leaving it dependent on the gate. With the flag off and a fixture that produces no caveat, it would pass for the wrong reason and become a gate that cannot fail. Set the flag; keep the test about mime types.

Add (the CSV mock setup to copy is at `BlobRow.test.tsx:79-83`; `:85-103` is that test's `render(...)` block, not its mock):

```tsx
  it("keeps the row/column counts out of the default DOM but KEEPS the structure caveat (elspeth-f1394307e3)", async () => {
    // Two assertions, opposite directions. The counts are engineer-register
    // and gate; the caveat is a data-honesty disclosure and does not — a body
    // the engine could not parse must not read as a clean preview at the
    // default detail level (see the caveat ruling).
    //
    // TRUNCATED, and the fixture choice is forced by contentStructure.ts —
    // it is the ONLY shape that carries a caveat AND a gateable summary line:
    //   ragged (:193-200)      rowCount null, fields null  -> describeStructuralSummary
    //                          returns null at EVERY level, so a "counts are
    //                          gated" assertion CANNOT FAIL. Vacuous.
    //   truncated (:203-209)   rowCount null, fields header -> caveat + a real
    //                          "columns: …" line. Gating IS observable. USE THIS.
    //   well-formed (:212)     rowCount n, fields header, caveat NULL -> no
    //                          caveat to keep. Covered by the next test.
    // No fixture carries a caveat AND a row count, which is why the row-count
    // half of the gate needs its own test below.
    (previewBlobContentSnippet as ReturnType<typeof vi.fn>).mockResolvedValue({
      text: "name,age\nAlice,30\nBob,40\n",
      truncated: true,
      limit: 5000,
    });
    const user = userEvent.setup();
    const { container } = render(<BlobRow blob={makeBlob({ mime_type: "text/csv" })} sessionId="session-1" onDownload={vi.fn()} onDelete={vi.fn()} onUseAsInput={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: /preview data\.csv/i }));
    await screen.findByText(/name,age/);                      // the preview body rendered
    // SCOPED to the block, the idiom this file already uses at :208-209. This
    // is also what disambiguates /truncated/i: the <pre>'s own
    // "... (truncated)" span (BlobRow.tsx:286-290) is OUTSIDE this element, so
    // an unscoped screen.getByText would throw "Found multiple elements".
    const summary = await screen.findByTestId("blob-row-structure");
    expect(summary).toHaveTextContent(/truncated/i);            // caveat survives
    expect(summary).not.toHaveTextContent(/columns: name, age/); // summary line gated
    // Audit-required siblings, per the Global Constraints list. The
    // RecoveryPanel flag-off test asserts its siblings; this one must too —
    // the flag-off render is a NEW code path and the existing tests now run
    // with the flag ON, so nothing else covers it.
    expect(screen.getByRole("button", { name: /download/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /delete/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /use as input/i })).toBeInTheDocument();
    expectNoIdentifiersInDefaultDom(container);
  });

  it("renders no structure block at all for a clean CSV at the default level, and the counts with the flag on (elspeth-f1394307e3)", async () => {
    // The row-count half of the gate. A well-formed CSV is the only shape with
    // a rowCount (contentStructure.ts:212), and it has caveat: null — so at the
    // default level BOTH children are absent and the tightened render
    // condition must drop the wrapper rather than emit an empty <div>.
    (previewBlobContentSnippet as ReturnType<typeof vi.fn>).mockResolvedValue({
      text: "name,age\nAlice,30\nBob,40\n",
      truncated: false,
      limit: 5000,
    });
    const user = userEvent.setup();
    render(<BlobRow blob={makeBlob({ mime_type: "text/csv" })} sessionId="session-1" onDownload={vi.fn()} onDelete={vi.fn()} onUseAsInput={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: /preview data\.csv/i }));
    await screen.findByText(/name,age/);
    expect(screen.queryByTestId("blob-row-structure")).not.toBeInTheDocument();

    // Same fixture, flag on: the counts appear. This is the assertion the
    // ragged fixture could never make, and it is what makes the gate
    // observable on the rowCount axis rather than only on the fields axis.
    usePreferencesStore.setState({ showAdvanced: true });
    const summary = await screen.findByTestId("blob-row-structure");
    expect(summary).toHaveTextContent(/\d+ rows?\b/);
    expect(summary).toHaveTextContent("columns: name, age");
  });
```

**The exemption question is settled here, not at implementation time.** A pre-fix draft told the implementing lane to add `allowSelectors: [".blob-row-preview"]` "if the fixture trips the scan" — deciding a gate exemption at implementation time, by the party who needs the test green, is how a gate erodes. The fixture is knowable now: the CSV preview text is `"name,age\nAlice,30\nBob,40\n"` (`BlobRow.test.tsx:81`), which contains no `snake_case` token, no UUID and no 32-hex string. **So `expectNoIdentifiersInDefaultDom(container)` is called with NO options and must pass as written. If it fails, that is new information about the fixture or the component — diagnose it and report; do not add an exemption to get green.** The fixture above asserts **three** buttons; the Global Constraint's BlobRow list is "status dot, creator badge and **four** actions". Read `BlobRow.tsx`'s actual accessible names, assert all four, and add the status dot and creator badge — the whole audit-required set for this row, not a sample of it.

`AuditReadinessPanel.test.tsx` — add `import { usePreferencesStore } from "@/stores/preferencesStore";` and `import { expectNoIdentifiersInDefaultDom } from "@/test/defaultDomPins";`, a `resetStore(usePreferencesStore)` line in the file's existing top-level `beforeEach` (`:153-165`), and `usePreferencesStore.setState({ showAdvanced: true });` at the top of the two tests that click Refresh (`:396` block and `:494`). Add:

```tsx
  it("hides Refresh with the flag off — the panel already refetches per composition version — and keeps Explain (elspeth-f1394307e3)", async () => {
    useAuditReadinessStore.setState({ snapshotsBySession: { [SESSION_ID]: allGreenSnapshot(1) } });
    useSessionStore.setState({ activeSessionId: SESSION_ID, compositionState: makeComposition(1) });
    const user = userEvent.setup();
    const { container } = render(<AuditReadinessPanel />);
    await user.click(screen.getByRole("button", { name: /Audit ready/i }));
    expect(screen.queryByRole("button", { name: "Refresh audit check now" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Explain what this pipeline will record" })).toBeInTheDocument();
    expectNoIdentifiersInDefaultDom(container);
  });

  it("still refetches on a composition-version change with the flag off (elspeth-f1394307e3 cadence guarantee)", async () => {
    // The ticket's acceptance has TWO halves: "each control absent from the
    // default DOM and present with the flag; AND no change to the
    // audit-readiness fetch cadence". Hiding Refresh is only honest because
    // the panel refetches itself, so that refetch IS the plain summary
    // standing in for the hidden control — and an unpinned plain summary is
    // exactly what the epic doctrine warns about. This is the pin.
    useSessionStore.setState({ activeSessionId: SESSION_ID, compositionState: makeComposition(1) });
    render(<AuditReadinessPanel />);
    await waitFor(() => expect(vi.mocked(api.fetchAuditReadiness)).toHaveBeenCalledTimes(1));
    act(() => {
      useSessionStore.setState({ compositionState: makeComposition(2) });
    });
    await waitFor(() => expect(vi.mocked(api.fetchAuditReadiness)).toHaveBeenCalledTimes(2));
  });
```

**The cadence test MUST supply two mock resolutions, or it pins the ERROR path.** The file automocks the module at `:17` (`vi.mock("../../api/auditReadiness")`), and every one of its ~15 fetch-exercising tests supplies a return (`:170,:183,:202,:216,:233,:245,:265,:292,:318,:337,:350,:382,:415`). Supplying none makes both calls resolve `undefined`; `auditReadinessStore.loadSnapshot` (`:111-118`) then fails its `matchingAuditReadinessSnapshot` identity check and lands in the retryable-error arm. **The call counts still reach 1 and 2, so the test passes — but it pins the cadence on the error path with an error banner rendered, not the success path the acceptance means, and it would break the moment the store grows any post-error refetch guard.** So:

```tsx
    vi.mocked(api.fetchAuditReadiness)
      .mockResolvedValueOnce(allGreenSnapshot(1))
      .mockResolvedValueOnce(allGreenSnapshot(2));
```

before the first `render`, matching the shape the file's other tests use.

The refetch `useEffect` depends on `compositionState?.version` (`AuditReadinessPanel.tsx:329-353`, the dependency at `:353` with its deliberate eslint suppression above it) — bump the VERSION, not some other field, or the effect will not re-run.

**Imports: both `act` and `waitFor` are already imported in this file — but `act` comes from `"react"` (`:2`), NOT from `@testing-library/react` (`waitFor` is at `:3`).** A pre-fix draft said to import both from RTL "if they are not already imported"; following that fallback literally would add the deprecated RTL `act`. Use what is there.

**The FIRST new test needs no mock, and that is not an inconsistency.** Its `beforeEach` (`:153-166`) resets the store with `getInitialState()`, the body then seeds `snapshotsBySession[SESSION_ID] = allGreenSnapshot(1)` at composition version 1, and `loadSnapshot` is documented as "a no-op when the cached snapshot's version matches the requested version" (`auditReadinessStore.ts:4-7`, guard at `:89-100`). No fetch fires.

Run: `npx vitest run src/components/recovery src/components/blobs src/components/audit/AuditReadinessPanel.test.tsx` → the four new tests FAIL (the cadence test may pass immediately, since it pins behaviour this task must PRESERVE rather than add — that is expected and correct; it fails only if the implementation breaks the cadence); the flag-on tests PASS.

- [ ] **Step 2: Implement**

`RecoveryPanel.tsx`: `import { useShowAdvanced } from "@/stores/preferencesStore";`; `const showAdvanced = useShowAdvanced();` after `:46` (legal there — the component's early return is at `:49`, *after* this point); wrap **`:158-176`** — the `recovery-panel-transcript-controls` div AND `<RecoveryTranscript …/>` — in `{showAdvanced && (<>…</>)}`. **Not through `:178`:** `:177` is the `</div>` closing the enclosing container, and wrapping it breaks the JSX. with the comment "Raw tool transcript is engineer-register (tool names, call ids, raw responses); RecoveryDiff + the two actions above/below are the audit-required summary and stay (elspeth-f1394307e3)."

`BlobRow.tsx`: import `useShowAdvanced`; `const showAdvanced = useShowAdvanced();` near `:141`. **Leave the `useMemo` at `:141-144` alone** — it must keep producing the summary so the caveat survives (see the caveat ruling). Gate the summary LINE instead, at `:145-147`:

```tsx
  // The row/column counts are structural introspection — engineer register,
  // gated (elspeth-f1394307e3). structuralSummary.caveat is NOT gated: a
  // ragged/unterminated-quote/oversized/unparseable body must disclose that at
  // every detail level, because nothing else in the row says so. (Truncation
  // is the one case the preview <pre> already covers with its own
  // "... (truncated)" span at :286-290; the rest have no counterpart.)
  const structuralSummaryLine = showAdvanced && structuralSummary
    ? describeStructuralSummary(structuralSummary)
    : null;
```

and tighten the block's render condition (`:263-266`) so it does not render an empty `<div>` when the counts are gated and there is no caveat:

```tsx
      {structuralSummary &&
        structuralSummary.format !== "unsupported" &&
        !previewLoading &&
        !previewError &&
        (structuralSummary.caveat !== null || structuralSummaryLine !== null) && (
```

(`caveat` is typed `string | null` — `utils/contentStructure.ts:48` — so the explicit `!== null` is right and a truthiness check would also swallow `""`, which that module never emits but which would be a silent behaviour change if it ever did.) Nothing else changes — the status dot, creator badge and four actions are untouched (`elspeth-50fd9b04e0` neutral-trigger rule).

`AuditReadinessPanel.tsx`: import `useShowAdvanced` (the file's hook block, `:14`); `const showAdvanced = useShowAdvanced();` with the other hooks (before any early return); wrap the Refresh `<Button …>` (**the tag opens at `:520` and closes at `:542`** — wrap from `:520`, not `:521`/`:522`, both of which are inside the opening tag) in `{showAdvanced && (…)}` with the comment "The panel refetches on every composition version (useEffect above); a manual Refresh is a debugging affordance (elspeth-f1394307e3). Explain and Collapse stay."

Run: `npx vitest run src/components/recovery src/components/blobs src/components/audit` → PASS; `npm run lint` → clean.

- [ ] **Step 3: Commit**

```bash
git add src/elspeth/web/frontend/src/components/recovery/RecoveryPanel.tsx src/elspeth/web/frontend/src/components/recovery/RecoveryPanel.test.tsx src/elspeth/web/frontend/src/components/blobs/BlobRow.tsx src/elspeth/web/frontend/src/components/blobs/BlobRow.test.tsx src/elspeth/web/frontend/src/components/audit/AuditReadinessPanel.tsx src/elspeth/web/frontend/src/components/audit/AuditReadinessPanel.test.tsx
git commit -m "feat(detail-level): recovery transcript, blob structural disclosure and audit Refresh render only with show_advanced (elspeth-f1394307e3)"
```

---

### Task 9: Delete the unknown-audit-characteristic chip (`elspeth-0bfd019f68`, remainder)

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/catalog/AuditCharacteristicIcon.tsx` (`:1-8` header, `:17-27` fallback)
- Modify: `src/elspeth/web/frontend/src/components/catalog/AuditCharacteristicIcon.test.tsx:38-48` (two tests: the raw-flag fallback at `:38-41` and the class assertion at **`:43-48`** — its closing `});` is at `:48`, not `:47`; `describe` closes at `:49`. BOTH go, the second because it asserts the very class this task deletes)
- Modify: `src/elspeth/web/frontend/src/components/catalog/catalogClassNames.test.ts:114-118` (`audit-icon-unknown` entry)
- Modify: `src/elspeth/web/frontend/src/styles/classNames.test.ts:306-309` (**the sibling `audit-icon-unknown` entry in the WHOLE-TREE gate** — see the ruling)
- Modify: `src/elspeth/web/frontend/src/components/catalog/PluginCard.tsx` (`:140-142` — the `lookupAuditCharacteristic` filter that stops an all-unknown plugin rendering an empty labelled group; see Step 2)
- Modify: `src/elspeth/web/frontend/src/components/catalog/PluginCard.test.tsx` (the three acceptance tests — see Step 1)

**Interfaces:** `AuditCharacteristicIcon` returns `null` for a flag with no metadata. The closed-set guard is `tests/unit/web/catalog/test_audit_characteristic_vocabulary_parity.py:38` (`test_audit_characteristic_python_matches_ts_metadata`) — it already fails CI on vocabulary drift, so the chip only ever fired when that gate was already red. `composer_tier_default` was deleted in Wave 1 (`elspeth-9cca900d41`) — do not touch it.

**Ruling — `audit-icon-unknown` is recorded in TWO allowlists and both entries die in this commit.** The name sits in `catalogClassNames.test.ts:114-118` AND in the whole-tree gate `src/styles/classNames.test.ts:306-309`, whose own reason text ends "Also recorded in catalogClassNames.test.ts" — i.e. the duplication is deliberate, a sibling record, not a copy to ignore. `classNames.test.ts`'s "keeps the rule-less allowlist honest" test (`:442-451`) asserts every allowlisted name is still applied by some product TSX (`componentSources` at `:374` walks the whole `src` tree). Deleting the class's only application site (`AuditCharacteristicIcon.tsx:21`, confirmed its only one) while leaving that entry standing fails that test with "allowlisted but no component applies it" — so a pre-fix draft of this task, which touched only the catalog allowlist, claimed a `PASS` on `src/styles/classNames.test.ts` that it could not have got. Cost if wrong: the whole-tree gate goes red for every sibling lane on the branch, which is the failure mode the Global Constraints call out by name. (`audit-icon-label` is ALSO in both allowlists and STAYS in both — the metadata branch that applies it is untouched.)

- [ ] **Step 1: Replace the two tests and pin the acceptance one level up**

`AuditCharacteristicIcon.test.tsx:38-48` — delete both tests (`"renders unknown flags as a fallback chip with the raw flag string"` at `:38-41` and `"applies an 'audit-icon-unknown' class for unknown flags"` at `:43-48`; the second closes at `:48`, not `:47`) and put this in their place:

```tsx
  it("renders nothing for a flag outside the closed vocabulary — drift is the parity test's job, not a chip's (elspeth-0bfd019f68)", () => {
    // future_characteristic, not the deleted tests' future_flag_2027: the
    // digit-free form is what the rest of this wave's fixtures use, because
    // SNAKE_RE admits no digits (Global Constraints). It makes no difference
    // to toBeEmptyDOMElement here, and it keeps one flag spelling in the wave.
    const { container } = render(<AuditCharacteristicIcon flag="future_characteristic" />);
    expect(container).toBeEmptyDOMElement();
  });
```

That is the mechanism pin. The wave's default-DOM acceptance pin goes on `PluginCard` instead — `AuditCharacteristicIcon`'s only consumer (`PluginCard.tsx:199`) and the surface where a raw backend flag would actually have reached a user. On the icon's own container the pin would scan an empty element and could not fail.

**Ruling — the PluginCard pin needs TWO tests, a real vocabulary member, a DIGIT-FREE unknown flag, and a positive assertion. A pre-fix draft's single test could not fail on any of those four axes.** Each defect was executed against the tree, not reasoned about:

1. **`records_llm_calls` is not in the vocabulary.** `AuditCharacteristicFlag` (`components/catalog/auditCharacteristics.ts:54-67`) is exactly: `io_read, io_write, external_call, deterministic, seeded, non_deterministic, provenance, retention, quarantine, coerce, signed, credentials`. After this task's change, a flag with no metadata renders `null` — so in the draft's fixture **both** flags rendered nothing and both assertions passed while proving nothing. (Note for anyone re-checking: a round-2 review report listed the vocabulary as `seeded_random, non_reproducible, type_coercion, retention_aware, extra_provenance, signed_output, requires_credentials`. Those members do not exist; the list above is from the file.)
2. **`future_flag_2027` cannot match `SNAKE_RE`.** The pin's regex is `/\b[a-z]+_[a-z_]+\b/` (`test/defaultDomPins.ts:16`); `[a-z_]+` admits no digits, and the trailing `\b` cannot be satisfied inside `future_flag_2027` — after matching `future_flag` the next character is `_` (a word character, no boundary), and backtracking to `flag_` leaves `2`, also a word character. Executed against the three live regexes: `"future_flag_2027"` → snake `false`; `"future_characteristic"` → snake `true`. **So `expectNoIdentifiersInDefaultDom` would have passed against a card that rendered `future_flag_2027` verbatim.** Use a digit-free out-of-vocabulary flag. (This is the same regex blind spot as the `\b` concatenation bug the helper's own header records at `56065e665`; it applies to any pin fixture in this wave that uses a trailing-digit identifier.)
3. **`io_read` is in the vocabulary but is NOT visible at the default detail level.** `PluginCard.tsx:140-142` filters to `showAdvanced || DEFAULT_VISIBLE_AUDIT_FLAGS.includes(flag)`, and `DEFAULT_VISIBLE_AUDIT_FLAGS` (`auditCharacteristics.ts:212-216`) is `["quarantine", "credentials", "external_call"]`. Use **`external_call`** (label `"network call"`, tone `attention`, `auditCharacteristics.ts:130-135`) as the in-vocabulary flag, so the known chip actually renders at the default level.
4. **Both assertions in the draft were negative**, so there was nothing anchoring them.

**And the reframing that follows from (3), which the ticket closeout must carry:** an unknown flag is not in `DEFAULT_VISIBLE_AUDIT_FLAGS` either, so it was filtered out *before* reaching the icon at the default level — **the unknown chip only ever rendered with `showAdvanced: true`.** "No raw backend flag in the default DOM" was therefore already true before this change. Deleting the chip is still correct (the Python↔TS parity test is the real drift guard, and a fallback chip could only ever have fired after that gate was already red), but it is a **detailed-level cleanup, not a default-DOM one.** Say that in the closeout rather than claiming a default-DOM fix.

Add to `PluginCard.test.tsx` (reuse the file's existing plugin fixture factory and props — read the file first; import `expectNoIdentifiersInDefaultDom` from `@/test/defaultDomPins` and `usePreferencesStore` from `@/stores/preferencesStore`):

```tsx
  it("shows the known chip and no raw flag at the DEFAULT detail level (elspeth-0bfd019f68)", () => {
    // external_call is in DEFAULT_VISIBLE_AUDIT_FLAGS, so its chip renders
    // here; future_characteristic is filtered out by that same list before it
    // ever reaches the icon. This test pins the default surface — it does NOT
    // exercise the deleted branch, which is what the next test is for.
    const { container } = render(
      <PluginCard plugin={makePlugin({ audit_characteristics: ["external_call", "future_characteristic"] })} />,
    );
    expect(screen.getByText("network call")).toBeInTheDocument();   // positive anchor
    expect(screen.queryByText("future_characteristic")).not.toBeInTheDocument();
    expectNoIdentifiersInDefaultDom(container);
  });

  it("renders no chip at all for an out-of-vocabulary flag at the DETAILED level (elspeth-0bfd019f68)", () => {
    // THIS is the test that exercises the deleted branch: with the flag on,
    // the unknown characteristic reaches AuditCharacteristicIcon, which now
    // returns null. Before the change it rendered `future_characteristic`
    // verbatim, so this assertion goes from red to green on the deletion.
    usePreferencesStore.setState({ showAdvanced: true });
    render(
      <PluginCard plugin={makePlugin({ audit_characteristics: ["external_call", "future_characteristic"] })} />,
    );
    expect(screen.getByText("network call")).toBeInTheDocument();
    expect(screen.queryByText("future_characteristic")).not.toBeInTheDocument();
  });
```

**`PluginCard.test.tsx` already calls the pin and already resets the preferences store** (it is one of the nine existing pin callers), so no new `beforeEach` is needed — confirm the reset is there before relying on `setState` in the second test, and add `resetStore(usePreferencesStore)` if it is not, or the flag leaks into every subsequent test in the file.

Run: `npx vitest run src/components/catalog/AuditCharacteristicIcon.test.tsx src/components/catalog/PluginCard.test.tsx` → **the SECOND PluginCard test FAILS** (the chip still renders `future_characteristic` at the detailed level), and the `AuditCharacteristicIcon` `toBeEmptyDOMElement()` test fails. The first PluginCard test passes already — it is a default-surface pin, not a red-phase test, and saying so here stops a reviewer reading its green as evidence of the fix.

- [ ] **Step 2: Delete the chip and its allowlist entry**

`AuditCharacteristicIcon.tsx`:

```tsx
// ============================================================================
// AuditCharacteristicIcon
//
// Single-flag renderer. Its ONE consumer is PluginCard.tsx:199 (sole import
// at :31) — the old header's "and the filter chip strip" was already wrong
// and is not carried forward. Looks up the flag in the centralised metadata
// table. A flag with no metadata renders NOTHING: the Python↔TS parity test
// (tests/unit/web/catalog/test_audit_characteristic_vocabulary_parity.py)
// fails CI on drift, so a fallback chip could only ever have shown a raw
// implementation flag to the user after the gate was already red
// (elspeth-0bfd019f68).
// ============================================================================

import { lookupAuditCharacteristic } from "./auditCharacteristics";

interface AuditCharacteristicIconProps {
  flag: string;
}

export function AuditCharacteristicIcon({ flag }: AuditCharacteristicIconProps) {
  const meta = lookupAuditCharacteristic(flag);
  if (meta === null) return null;
  return (
    <span
      className={`audit-icon audit-icon-${meta.tone}`}
      title={meta.tooltip}
    >
      <span className="audit-icon-label">{meta.label}</span>
    </span>
  );
}
```

Then delete the entry from **both** allowlists in this same commit (see the ruling — the whole-tree gate's copy is a deliberate sibling record, and leaving it turns `classNames.test.ts` red for every lane on the branch):

- `catalogClassNames.test.ts:114-118` — the `"audit-icon-unknown": …` entry and its four reason lines.
- `src/styles/classNames.test.ts:306-309` — the `"audit-icon-unknown": …` entry and its three reason lines, from the `RULE_LESS_BY_DESIGN` map. Leave `"audit-icon-label"` (`:311-313`) alone in both files; the metadata branch still applies it.

**Also filter unknown flags out before `PluginCard`'s `length > 0` guard**, or the card can render a labelled group with no children. With `showAdvanced` on and a plugin whose advanced characteristics are ALL outside the vocabulary, `visibleAuditCharacteristics.length > 0` (`PluginCard.tsx:189`) is true, every `AuditCharacteristicIcon` now returns `null`, and the result is `<div role="group" aria-label="Audit characteristics">` with nothing inside — announced as a labelled group containing nothing. One clause at `PluginCard.tsx:140-142`:

```tsx
  const visibleAuditCharacteristics = [...plugin.audit_characteristics]
    .filter((flag) => showAdvanced || (DEFAULT_VISIBLE_AUDIT_FLAGS as readonly string[]).includes(flag))
    // An unknown flag renders nothing (AuditCharacteristicIcon), so leaving it
    // in the list produces an EMPTY role="group" with an aria-label — a group
    // announced as containing nothing. Drop it here instead.
    .filter((flag) => lookupAuditCharacteristic(flag) !== null)
    .sort();
```

(Import `lookupAuditCharacteristic` from `./auditCharacteristics`.)

**This needs its OWN third test, not an assertion bolted onto the second one.** The second test's fixture is `["external_call", "future_characteristic"]`, which after this filter renders a group containing one chip — the group IS present, so a "group absent" assertion there is simply false and an implementer following it literally would write a failing assertion and then weaken something to "fix" it. The empty-group case needs a fixture whose flags are ALL unknown:

```tsx
  it("renders no labelled group at all when every characteristic is out of vocabulary (elspeth-0bfd019f68)", () => {
    // PluginCard.tsx:189's `length > 0` guard counts flags, not rendered
    // chips. Without the lookup filter this is a <div role="group"
    // aria-label="Audit characteristics"> with no children — announced as a
    // labelled group containing nothing.
    usePreferencesStore.setState({ showAdvanced: true });
    render(<PluginCard plugin={makePlugin({ audit_characteristics: ["future_characteristic"] })} />);
    expect(screen.queryByRole("group", { name: "Audit characteristics" })).not.toBeInTheDocument();
  });
```

Verify the class is gone tree-wide and that no stylesheet rule ever existed for it: `git grep -n "audit-icon-unknown" -- src` returns **five LINES across four files** before the change — component `AuditCharacteristicIcon.tsx:21`, both allowlists, and `AuditCharacteristicIcon.test.tsx` twice — **`:43` and `:47`, BOTH inside the SECOND deleted test (`:43-48`): `:43` is its `it(...)` title, `:47` its assertion. The FIRST deleted test (`:38-41`) does not name the class at all.** A lane checking "four" against `git grep -n` output will see five and think something is wrong; it is the two hits in one file, in one test. There is no `.audit-icon-unknown` rule in any stylesheet (all five hits are TS/TSX), so deleting both allowlist entries is clean.

Run: `npx vitest run src/components/catalog src/styles/classNames.test.ts` → PASS, including `classNames.test.ts`'s "keeps the rule-less allowlist honest" test (`:442-451`), which is the test that would have caught a one-sided deletion. Backend guard unchanged: `pytest tests/unit/web/catalog/test_audit_characteristic_vocabulary_parity.py -q` → PASS.

- [ ] **Step 3: Commit**

```bash
git add src/elspeth/web/frontend/src/components/catalog/AuditCharacteristicIcon.tsx \
        src/elspeth/web/frontend/src/components/catalog/AuditCharacteristicIcon.test.tsx \
        src/elspeth/web/frontend/src/components/catalog/PluginCard.tsx \
        src/elspeth/web/frontend/src/components/catalog/PluginCard.test.tsx \
        src/elspeth/web/frontend/src/components/catalog/catalogClassNames.test.ts \
        src/elspeth/web/frontend/src/styles/classNames.test.ts
git status --short   # MUST be clean of catalog/ leftovers
git commit -m "refactor(catalog): delete the unknown-audit-characteristic fallback chip and both rule-less allowlist entries; parity test is the drift guard (elspeth-0bfd019f68)"
```

Ticket closes at Task 11 (its other item was Wave 1's).

---

### Task 10: Preferences payload decoder (`elspeth-7d07df6438`, bug, absorbed)

**Files:**
- Create: `src/elspeth/web/frontend/src/api/preferencesDecoder.ts` + `preferencesDecoder.test.ts`
- Modify: `src/elspeth/web/frontend/src/api/client.ts:754-771` (`fetchUserComposerPreferences`, `updateUserComposerPreferences`)

**Interfaces:**
- Produces: `decodeUserComposerPreferences(value: unknown): UserComposerPreferencesPayload` — exact record (`interface UserComposerPreferencesPayload`, `types/api.ts:90-110`: ten keys; the keys and their types are exact, only the line range in a pre-fix draft was not), throws `Error("Invalid composer preferences at <path>: <detail>")` on a missing/extra key or wrong type. Both client functions call it on the parsed body; `parseResponse<unknown>` stays the transport (its 401 interceptor at `:212-216` is untouched).
- Consumes: the `guidedDecoder.ts` idiom (`:102-128`: `invalid`/`record`/`exactRecord`; `:171-178`: `nullableString`/`booleanValue`) — **copied, not imported. This is a known, measured duplication, parked with a ticket, not a clean reuse; see the ruling below.**

**Ruling — the decoder primitives are COPIED in this wave, and that is recorded as a second instance of the archetype rather than presented as clean.** A structural sweep found this is the same shape as the Task 3 topology defect: `api/guidedDecoder.ts` holds eight private primitives (`invalid` `:102`, `record` `:106`, `exactRecord` `:113`, `stringValue` `:130`, `nullableString` `:171`, `booleanValue` `:175`, `integerValue` `:180`, `stringArray` `:190`) and exports only its four `decode*Response` functions (`:2216`, `:2220`, `:2264`, `:2268`), so a second decoder consuming *those* primitives can only copy them. The right fix is a `src/api/decodePrimitives.ts` leaf with the error prefix injected. It is NOT done here, and the reason is measured rather than asserted:

- The eight primitives are called **487 times** inside `guidedDecoder.ts` (`invalid` alone 173, `exactRecord` 92, `stringValue` 134 — counted 2026-08-30).
- `invalid` hardcodes its prefix in its own body: `` throw new Error(`Invalid guided response at ${path}: ${detail}`) ``. Parameterising it means either threading a prefix through all 173 call sites or converting the module to a factory bundle, which changes how all 487 sites resolve their helpers.
- The cheap-looking shortcut is worse than the copy: simply exporting the primitives unchanged would give the preferences decoder error messages reading "Invalid **guided response** at show_advanced", i.e. a decoder that lies about which payload failed. A copy with an honest prefix beats a shared helper with a false one.

That is a refactor of a **2289-line** validation module (`api/guidedDecoder.ts`; the figure 2270 in a pre-fix draft was wrong, twice) inside a wave whose scope is copy register, so it is parked (Roadmap tail) rather than absorbed.

**Correct the SCOPE of the park as well as its reason — "a second decoder can only copy them" is not what the tree shows, and the 487-call measurement covers one of FOUR sites.** The frontend already holds two more structural-decoder primitive copies:

- `api/auditReadiness.ts:39` `unexpectedShape(status)` + `:46` `isRecord(value)`
- `api/shareableReviews.ts:32` `unexpectedShape(status, where, cause?)` + `:50` `isRecord(value)`

and **both throw `ApiError` objects, not `Error`** — a third error contract alongside `guidedDecoder`'s `Invalid guided response at <path>` and this task's proposed `Invalid composer preferences at <path>`. So Task 10 makes the **fourth** copy, not the second, and the parked `decodePrimitives.ts` ticket must be scoped to all four **and must decide the error contract** (`ApiError` for transport-layer decoders vs `Error` for store-layer ones). Otherwise a future `decodePrimitives.ts` unifies two of the four and leaves standing the split it was written to close. The Roadmap row carries this scope; Task 11 Step 5 files it.

What changes here is only the honesty of the record: this is another instance of an archetype the frontend already has three of, and it is named as such. Cost if wrong: four decoders drift in their error-message shape or their exact-record strictness until the ticket lands — bounded, because each is pinned by its own tests.

**Ruling — fail closed, no `undefined` into the store. The DEFECT is real; a pre-fix draft's description of its SYMPTOM was not, and that description would have been committed into a source-file header.** The draft said: "`show_advanced` omitted → `undefined` → `<details open={undefined}>` closed by accident, a silent wrong default on every Wave 1/2 surface." That does not occur. React renders `open={undefined}` and `open={false}` identically — the attribute is omitted and the `<details>` is closed — and the store's declared default IS `false` (`preferencesStore.ts:152`). Every consumer coerces: `<details open={showAdvanced}>` at `SchemaFormTurn.tsx:207,:230`, `WireStageTurn.tsx:436,:467,:510`, `ProgressView.tsx:108,:334`, `OptionRows.tsx:201`; `!showAdvanced` at `ValidationResult.tsx:191,:210`; `checked={!showAdvanced}` at `ComposerPreferencesPanel.tsx:167`. **With `undefined` in the store, not one of those closes "by accident" — every one renders exactly as it does at the intended default.**

**The real, distinguishable symptom is one line further down: `ComposerPreferencesPanel.tsx:178`, `checked={showAdvanced}` on the "Show technical detail" radio.** `checked={undefined}` makes that input **uncontrolled** in React: it emits the "changing an uncontrolled input to be controlled" warning and the radio decouples from the store, while its `checked={!showAdvanced}` sibling at `:167` stays controlled. So the Detail-level control silently stops reflecting the preference it names — a genuine user-visible defect, and exactly what Task 11 Step 4 Check 5 exercises ("flip the in-app 'Detail level' radio"). The two would have collided.

The decoder is unaffected and still right — this is a defect in the *justification*, not the fix. But committing an invented mechanism into a source-file header is what the project's no-fabrication doctrine forbids, so **the header comment in Step 2 and the `root_cause` field in Task 0 Step 2 both carry the corrected statement**: a `boolean`-declared store field silently holding `undefined`, whose one distinguishable consumer is the uncontrolled radio at `ComposerPreferencesPanel.tsx:178`.

The defect itself: A thrown decode error surfaces in `preferencesStore.bootstrap`'s existing error path (the store's `error` field / banner cluster, `preferencesStore.test.ts:682`). Cost if wrong: a backend that legitimately adds a field breaks preference loading until the decoder learns it — the exact-record discipline every other decoder in this frontend already imposes.

**State the blast radius honestly, because it is wider after Task 8 than "preference loading" suggests.** `preferencesStore.ts:152` initialises `showAdvanced: false`, and `bootstrap`'s catch (`:178-204`) deliberately leaves every preference field unset — its comment says setting them would "attribute a preference choice to the user that they never made" — recording only the error. So **any** decode throw leaves `showAdvanced === false`. After Task 8 that collapses not only the Wave 1/2 `<details open={showAdvanced}>` surfaces but the recovery transcript, the blob structural counts and the audit Refresh as well. It is not *silent* — the catch drives a `role="alert"` banner ("Couldn't load your preferences (…)"), and `defaultMode` staying null produces the "you're in freeform" message downstream — but a user in the failure state sees a correct-looking plain UI plus a banner about something else, and Task 8's own ruling is "omit, don't hint — the sibling content IS the plain summary". That is the trade: a loud banner and a conservative UI, versus `undefined` silently in a `boolean` field. **Taken deliberately; the store-level test above makes the consequence observable rather than asserted.** Deploy skew is not an additional risk here: the frontend build is served by the same service that serves the API, so a backend field addition and its frontend decoder ship together.

- [ ] **Step 1: Failing decoder tests**

```ts
import { describe, expect, it } from "vitest";
import { decodeUserComposerPreferences } from "./preferencesDecoder";

const full = {
  default_mode: "guided",
  banner_dismissed_at: null,
  freeform_intro_dismissed_at: "2026-05-19T12:00:00Z",
  tutorial_completed_at: null,
  tutorial_stage: "run",
  tutorial_session_id: "sess-1",
  tutorial_run_id: null,
  tutorial_source_data_hash: null,
  show_advanced: true,
  updated_at: null,
};

describe("decodeUserComposerPreferences", () => {
  it("accepts the exact payload", () => {
    expect(decodeUserComposerPreferences(full)).toEqual(full);
  });
  it("rejects an omitted show_advanced instead of loading undefined (elspeth-7d07df6438)", () => {
    const { show_advanced: _omitted, ...without } = full;
    expect(() => decodeUserComposerPreferences(without)).toThrow(/missing show_advanced/);
  });
  it("rejects a non-boolean show_advanced, an unknown mode, an unknown stage, and an extra key", () => {
    expect(() => decodeUserComposerPreferences({ ...full, show_advanced: "yes" })).toThrow(/show_advanced/);
    expect(() => decodeUserComposerPreferences({ ...full, default_mode: "wizard" })).toThrow(/default_mode/);
    expect(() => decodeUserComposerPreferences({ ...full, tutorial_stage: "welcome" })).toThrow(/tutorial_stage/);
    expect(() => decodeUserComposerPreferences({ ...full, extra: 1 })).toThrow(/unexpected extra/);
  });
  it("rejects a non-object payload", () => {
    // exactRecord's "expected object" arm. A 401 interceptor or a proxy can
    // hand back null, an array or an empty body; none of those is a payload.
    for (const value of [null, [], "", 0]) {
      expect(() => decodeUserComposerPreferences(value)).toThrow(/expected object/);
    }
  });
  it("accepts tutorial_stage: null — the COMMON production shape", () => {
    // No tutorial in progress. This is the one branch of the enum guard the
    // negative cases never walk (`stage !== null && ...`), and it is what
    // most real accounts send.
    const noTutorial = { ...full, tutorial_stage: null, tutorial_session_id: null };
    expect(decodeUserComposerPreferences(noTutorial)).toEqual(noTutorial);
  });
});
```

**One store-level test as well, in `src/stores/preferencesStore.test.ts`,** pinning the interaction rather than each half — the decoder tests above cover the decoder in isolation, and the consequence of a rejection is what the fail-closed ruling actually trades on:

```tsx
  it("leaves showAdvanced false and surfaces the error when the payload is rejected (elspeth-7d07df6438)", async () => {
    vi.mocked(client.fetchUserComposerPreferences).mockRejectedValueOnce(
      new Error("Invalid composer preferences at $: missing show_advanced"),
    );
    await usePreferencesStore.getState().bootstrap();
    expect(usePreferencesStore.getState().showAdvanced).toBe(false);
    expect(usePreferencesStore.getState().error).not.toBeNull();
  });
```

(Read `preferencesStore.ts:178-204` for the exact error field name and the mocked client symbol before writing this — the catch is documented as deliberately leaving every preference field unset so it cannot "attribute a preference choice to the user that they never made", and records only the write/load error.)

Run: `npx vitest run src/api/preferencesDecoder.test.ts` → FAIL (module missing).

- [ ] **Step 2: Implement**

```ts
// ============================================================================
// preferencesDecoder — structural decoder for the account-level composer
// preferences payload (elspeth-7d07df6438). `parseResponse` (api/client.ts:208)
// ends in an unchecked `as T` cast, so before this decoder an omitted
// `show_advanced` loaded `undefined` into a field preferencesStore declares
// `boolean`.
//
// Be precise about the symptom, because the obvious guess is wrong: React
// renders `open={undefined}` and `open={false}` identically, so no
// `<details open={showAdvanced}>` closed "by accident" — every coercing
// consumer rendered as it does at the intended default. The one
// distinguishable consumer is ComposerPreferencesPanel.tsx:178,
// `checked={showAdvanced}`: `checked={undefined}` makes that radio
// UNCONTROLLED, so it decouples from the store and stops reflecting the
// preference, while its `checked={!showAdvanced}` sibling at :167 stays
// controlled. That is the user-visible defect this decoder closes.
//
// Same exact-record discipline as api/guidedDecoder.ts.
// ============================================================================

import type {
  ComposerMode,
  PersistedTutorialStage,
  UserComposerPreferencesPayload,
} from "@/types/api";

// Records keyed by the union, NOT Sets built from a literal array. A
// `Set<T>` built from an array does not fail the build when T gains a
// member: adding "kiosk" to ComposerMode would leave this decoder silently
// REJECTING a now-valid payload, and because the decoder fails closed that
// rejection breaks preference loading entirely. A Record keyed by the union
// makes the same addition a compile error here — the standard Task 4's
// phrase-map ruling sets, applied to the same archetype.
//
// The backend carries a lockstep-extension covenant naming the call sites
// that must move together (web/preferences/models.py:20-32, :44-51); this
// decoder would otherwise become a silent fourth.
const MODES: Record<ComposerMode, true> = { guided: true, freeform: true };
const STAGES: Record<PersistedTutorialStage, true> = {
  guided: true,
  run: true,
  audit: true,
  graduation: true,
};
const KEYS = [
  "default_mode",
  "banner_dismissed_at",
  "freeform_intro_dismissed_at",
  "tutorial_completed_at",
  "tutorial_stage",
  "tutorial_session_id",
  "tutorial_run_id",
  "tutorial_source_data_hash",
  "show_advanced",
  "updated_at",
] as const;

function invalid(path: string, detail: string): never {
  throw new Error(`Invalid composer preferences at ${path}: ${detail}`);
}

function exactRecord(value: unknown, path: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) invalid(path, "expected object");
  const result = value as Record<string, unknown>;
  for (const key of KEYS) {
    if (!Object.prototype.hasOwnProperty.call(result, key)) invalid(path, `missing ${key}`);
  }
  const allowed: ReadonlySet<string> = new Set(KEYS);
  for (const key of Object.keys(result)) {
    if (!allowed.has(key)) invalid(path, `unexpected ${key}`);
  }
  return result;
}

function nullableString(value: unknown, path: string): string | null {
  if (value === null) return null;
  if (typeof value !== "string") invalid(path, "expected string or null");
  return value;
}

function booleanValue(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") invalid(path, "expected boolean");
  return value;
}

export function decodeUserComposerPreferences(value: unknown): UserComposerPreferencesPayload {
  const path = "composer-preferences";
  const r = exactRecord(value, path);
  const mode = r.default_mode;
  if (typeof mode !== "string" || !Object.prototype.hasOwnProperty.call(MODES, mode)) {
    invalid(`${path}.default_mode`, "expected guided|freeform");
  }
  const stage = r.tutorial_stage;
  if (stage !== null && (typeof stage !== "string" || !Object.prototype.hasOwnProperty.call(STAGES, stage))) {
    invalid(`${path}.tutorial_stage`, "expected guided|run|audit|graduation|null");
  }
  return {
    default_mode: mode as ComposerMode,
    banner_dismissed_at: nullableString(r.banner_dismissed_at, `${path}.banner_dismissed_at`),
    freeform_intro_dismissed_at: nullableString(r.freeform_intro_dismissed_at, `${path}.freeform_intro_dismissed_at`),
    tutorial_completed_at: nullableString(r.tutorial_completed_at, `${path}.tutorial_completed_at`),
    tutorial_stage: stage as PersistedTutorialStage | null,
    tutorial_session_id: nullableString(r.tutorial_session_id, `${path}.tutorial_session_id`),
    tutorial_run_id: nullableString(r.tutorial_run_id, `${path}.tutorial_run_id`),
    tutorial_source_data_hash: nullableString(r.tutorial_source_data_hash, `${path}.tutorial_source_data_hash`),
    show_advanced: booleanValue(r.show_advanced, `${path}.show_advanced`),
    updated_at: nullableString(r.updated_at, `${path}.updated_at`),
  };
}
```

(`Object.prototype.hasOwnProperty.call` is the idiom `guidedDecoder.ts:121` and `ValidationResult.tsx:75` already use — it is not a `getattr`-class probe and the masquerade gate does not scan TypeScript.)

`client.ts:754-771`: both functions become `return decodeUserComposerPreferences(await parseResponse<unknown>(response));` with `import { decodeUserComposerPreferences } from "./preferencesDecoder";`. Check `src/api/client.*.test.ts` for existing preference-fetch mocks that return partial payloads and complete them (grep `composer-preferences`); `preferencesStore.test.ts` mocks the client functions themselves, so it is unaffected.

Run: `npx vitest run src/api src/stores/preferencesStore.test.ts src/components/settings` → PASS; `npx tsc --noEmit -p tsconfig.app.json` → clean.

- [ ] **Step 3: Commit + bug workflow**

```bash
git add src/elspeth/web/frontend/src/api/preferencesDecoder.ts \
        src/elspeth/web/frontend/src/api/preferencesDecoder.test.ts \
        src/elspeth/web/frontend/src/api/client.ts \
        src/elspeth/web/frontend/src/stores/preferencesStore.test.ts
git commit -m "fix(preferences): decode the account-preferences payload structurally on GET and PATCH — an omitted show_advanced fails closed (elspeth-7d07df6438)"
filigree update elspeth-7d07df6438 --actor <lane> --status verifying \
  -f fix_verification="preferencesDecoder.test.ts covers six independent failure axes (omitted key, wrong type, unknown enum value, extra key, non-object payload, and the tutorial_stage:null common shape) plus the happy path, and preferencesStore.test.ts pins that a rejected payload leaves showAdvanced false with the error surfaced; client.ts wired on both GET and PATCH; npx vitest run src/api src/stores/preferencesStore.test.ts src/components/settings green; npx tsc --noEmit -p tsconfig.app.json clean. Live: Task 11 Check 5 (Detail level radio round-trips)."
```

**`fix_verification` is not optional and must be set in THIS command.** `verifying → closed` is the bug template's one HARD transition and requires that field (`filigree type-info bug`), and `filigree close` has no `--field`/`-f` option to supply it later — so a Task 11 close loop that reaches this ticket without it aborts with `Cannot transition 'verifying' -> 'closed' for type 'bug': missing required fields: fix_verification`. `filigree update` applies the status change and the fields atomically, which is why they go together here. (`severity` and `root_cause` were set at claim time, Task 0 Step 2.)

Close at Task 11 after the full suite.

---

### Task 11: Whole-tree verification, live check, and closeout

**Files:** none new. Runs on the merged integration branch, not on any single lane's branch.

**This task is designed to be RE-RUN after a partial failure, so every step below is idempotent or says how to make it so.** A pre-fix draft was not: it added a worktree unguarded, closed eight tickets unconditionally, and wrote its lint baseline to a bare `/tmp` name with no recovery path.

- [ ] **Step 0: Confirm the integration branch is actually complete**

Step 1a opens "all task branches landed" and a pre-fix draft verified that nowhere. Given the shared-checkout hazards this plan catalogues by name — a sibling's `git add -A` sweeping staged files, a failed hook leaving files staged, a silent revert — enumerate before gating:

```bash
git log --oneline main..HEAD | grep -cE 'elspeth-(93f5621f18|d74ab492dd|4bf65fe149|d1feee1e67|f1394307e3|0bfd019f68|13b69b5846|59631ec7f7|7d07df6438)'
git log --oneline main..HEAD          # read it; every task's named commit must be present
git status --short                    # must be clean before any gate runs
```

Also record the vitest test count now, before the gates: `npx vitest run 2>&1 | tail -5`. **"All green" cannot distinguish a passing suite from a shrunken one** — Task 9 deletes two tests and adds two, Task 2 relocates one, so the arithmetic is knowable: record before and after and reconcile the delta against those three edits. Count, never `tail` alone.

- [ ] **Step 1a: Frontend full run**

From `src/elspeth/web/frontend`:

```bash
npx vitest run
npx tsc --noEmit -p tsconfig.app.json     # clean
npx tsc --noEmit -p tsconfig.test.json    # clean
npx tsc --noEmit -p tsconfig.e2e.json     # ONE standing error, see below
npm run lint
npm run lint:css
npm run build                             # a GATE, not a live-check prerequisite
```

**Run the three `tsc` invocations SEPARATELY, not chained with `&&`, and expect the e2e config to be non-clean.** `tsconfig.e2e.json` includes `tests/e2e/**/*.ts`, which pulls in `tests/e2e/harness/staging-tutorial-driver.test.ts:8` and produces exactly one error, every time, at every commit:

```
tests/e2e/harness/staging-tutorial-driver.test.ts(8,8): error TS7016: Could not find a declaration
file for module '../../../scripts/staging-tutorial-driver.mjs'
```

That is `elspeth-062c1d0b7f` (parked; its body names this TS7016 explicitly), **not** a Wave 3 regression. Task 7 Step 2 already discloses it; a pre-fix draft of this step did not, and chained the three configs with `&&` so the e2e failure masked whether the app and test configs ran at all. **Acceptance: app and test configs clean; e2e config shows that one TS7016 and nothing else.** Any second e2e error is a finding.

**`npm run build` is a GATE and belongs here, not in Step 4.** A pre-fix draft ran it only inside the live check, i.e. after the closeout gates had already passed. Tasks 3 and 10 each create a new module imported across a directory boundary (`lib/graphTopology.ts` into `components/inspector` and `components/workspace`; `api/preferencesDecoder.ts` into `api/client.ts`), and a vite production build can fail on resolution or bundling where all three `tsc --noEmit` runs pass. Step 4 still runs `npm run build` to produce the deployed artefact; this run is the gate.

Expected otherwise: all green, including `src/styles/classNames.test.ts` and its "keeps the rule-less allowlist honest" test at `:442-451` — **which is what Task 9's two-sided allowlist deletion is graded on** — plus `catalogClassNames.test.ts`, `executionClassNames.test.ts`, the new `src/test/defaultDomPins.test.ts` (Task 2), and every default-DOM pin added in Tasks 1, 2, 4, 5, 8 and 9. (A pre-fix draft called `catalogClassNames.test.ts` "the allowlist-honesty test after **Task 8**". The allowlist edit is **Task 9**; Task 8 touches no allowlist; and the honesty test itself lives in `src/styles/classNames.test.ts`, not in the catalog file. Both files must be green — only the labelling was wrong, and given how this plan's other misdirection played out, a wrong task number on a verification checklist is worth correcting.)

- [ ] **Step 1b: Frontend e2e (sequential, one worktree)**

`npx playwright test tests/e2e/composer-workspace-graph-keyboard.spec.ts tests/e2e/composer-workspace-accessibility.spec.ts tests/e2e/composer-preferences.spec.ts` — the new spec, axe over the changed surfaces, and the preference round-trip (Task 10 changed the decoder both fetch paths use). Do not include `composer-workspace-geometry.spec.ts`'s tall-dialog scenario as a Wave 3 gate (`elspeth-71bbf7eb12`, P1, out of scope); if the geometry spec is run and only `assertTallDialogLivePreflight` times out, record it under that ticket, not this wave.

**Why three specs is the whole e2e blast radius, stated so a reviewer need not re-derive it:** the wave changes visible strings, so the exposure is e2e specs that PIN those strings. Checked 2026-08-30 — `composer-workspace-geometry.spec.ts:122` asserts only that `acknowledgement-card` is VISIBLE, never its title; `composer-workspace-accessibility.spec.ts:151` is `const spec = composer.artifactTab("Spec");` — a locator declaration; that spec opens the Spec tab and asserts no heading text anywhere; and no e2e spec anywhere pins a run-confirm egress sentence or a Spec-tab `<h4>`. So no existing spec can go red on a copy change, and the three above are run because they exercise the surfaces this wave ADDS or rewires, not because the rest are at risk.

- [ ] **Step 2: Lint corpus diff (backend-touching: Task 6 only)**

**Confirm Task 7's lane has landed and no Playwright run is in flight before Step 1b** — Playwright auth state is worktree-global and two concurrent runs corrupt it; Task 7's lane and this step both run Playwright and a pre-fix draft never sequenced them.

```bash
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  elspeth-lints check --rules all --root src/elspeth > "$W3_CORPUS_DIR/after.txt"
diff "$W3_CORPUS_DIR/before.txt" "$W3_CORPUS_DIR/after.txt"
grep -c . "$W3_CORPUS_DIR/after.txt"   # stdout only; grep -c exits 1 on zero — do not wrap in set -e
```

Expected: no added findings, identical counts (COUNT the corpus, never `tail` it). `$W3_CORPUS_DIR` is Task 0 Step 1's durable path.

**If `$W3_CORPUS_DIR/before.txt` is missing, do NOT substitute the Wave 2 figure of 2354 and do NOT skip the diff** — the constraint is fail-closed and an unverifiable diff is a failed one. Re-create the baseline from the recorded branch-point sha using the worktree recipe in Task 0 Step 1, and say in the epic comment that the baseline was reconstructed rather than captured.

- [ ] **Step 3: Backend full suite as a background worktree job**

**Guarded worktree add and an explicit teardown — a bare `git worktree add` aborts with "already exists" on the second pass, and a pre-fix draft guarded the symlink but not the add, and had no cleanup at all:**

```bash
git worktree remove --force .claude/worktrees/wave3-verify 2>/dev/null || true
git worktree add .claude/worktrees/wave3-verify HEAD
ln -s "$(pwd)/.venv" .claude/worktrees/wave3-verify/.venv 2>/dev/null || true
cd .claude/worktrees/wave3-verify
PYTHONPATH=$(pwd)/src:$(pwd)/elspeth-lints/src .venv/bin/python -c "import elspeth, elspeth_lints; print(elspeth.__file__, elspeth_lints.__file__)"  # both must point into the worktree
PYTHONPATH=$(pwd)/src:$(pwd)/elspeth-lints/src .venv/bin/python -m pytest tests/ -n 12 -q 2>&1 | tail -30
# teardown, once the run has been read:
cd - && git worktree remove .claude/worktrees/wave3-verify
```

(`git worktree remove --force … || true` at the top is safe on a first pass — there is nothing to remove — and makes the second pass idempotent. The `.venv` symlink is recreated with the add.)

Run in the background; **read the summary line, not `tail` alone** (confirm a non-zero collected count — a bad path makes xdist collect zero and report success). Must-be-green list:

- `tests/unit/web/composer/test_graph_topology_parity.py` — **Task 3's re-anchored parity gate. This is the module Task 3 breaks if Step 2b was skipped, and a pre-fix draft's list omitted it: the one gate this wave actually disturbs.**
- `tests/unit/web/composer/test_producer_resolver.py` — the Python authority behind it
- `tests/unit/web/composer/test_prompts.py`, `test_tool_declarations.py`, `test_prompt_cache_layout.py`, `test_capability_skill_identity.py`, `test_skills_loader.py`, `test_compose_loop_interpretation_review_dispatch.py`
- `tests/unit/web/catalog/test_audit_characteristic_vocabulary_parity.py`
- `tests/unit/web/test_sessions_composer_attribute_contracts.py`
- `tests/unit/elspeth_lints/test_masquerade_gate.py`
- knob-schema goldens untouched (`git status --short tests/golden` empty)

- [ ] **Step 4: Live check on session 39578c6f**

Executor: the hub, on the merged integration branch. `npm run build` in `src/elspeth/web/frontend`; because Task 6 changed a Python-packaged file (`skills/pipeline_composer.md` is read at import via `load_skill_with_hash`), restart with the exact sudoers form `sudo -n /usr/bin/systemctl restart elspeth-web.service`, then poll `/api/system/status` until `frontend_build` shows the new build id (`is-active` lies after a restart). Site https://elspeth.foundryside.dev, throwaway eval login `<staging-username>` / `<staging-password>`. An API PATCH of preferences does not reach a mounted store — flip the in-app "Detail level" radio for the flag-on pass.

1. **Spec tab at the default preference** (the wave's acceptance): walk every card on 39578c6f v19; count snake_case text nodes outside `<code>`/`<pre>`/`<details>` and compare with the Wave 1 epic-comment baseline (Task 0 Step 3). **That baseline is 40 — 12 node-id headings, 23 branch/wire-stage values, 5 policy/plugin vocab — and NOT the figure 83**, which was a pre-fix raw count from a gitignored working artefact that no longer exists. The further **37 reader-data items (column names, prompt text) are verbatim by design and OUT OF SCOPE**; do not chase them, and do not count them as failures. Expected: the 12 node-id headings, the 23 branch/wire-stage values and the 5 policy/plugin vocab items are gone from visible text; hovering a heading or routing value shows the raw id in the tooltip. Remaining hits must be one of: OptionRows `field_mapping`/user-keyed map keys (verbatim by design, Wave 1), or prompt-template excerpt field names (`case_study1` — authored content, ruling below). Anything else REOPENS `elspeth-93f5621f18`.
2. **Acknowledgement card for a deleted node:** delete a node that has a pending card (or use a session with one); the card title reads "Removed step · …"; the wire-blocker jump link matches; the DOM node carries `data-affected-node-id`.
3. **Freeform reply register:** in a fresh freeform session ask for a two-branch pipeline with a coalesce; the final reply names steps by label, says "waits for every branch" (or equivalent) rather than `require_all`, says "validation passed" rather than `is_valid: true`, and contains no ASCII topology tree. This is a planner-behaviour observation, not a pass/fail gate — record the reply verbatim in the epic comment; if it still leaks, the brief needs another turn (a new ticket), not a server-side filter.
4. **Register batch:** header chip shows "Composer: Claude Sonnet 5" (or the deployment's model) with the raw id on hover; Secrets panel badges read Yours/Deployment; run-confirm dialog lines read "Source (CSV)", "Fetch Page (Web Scrape)" with the identifier sentence on hover; Explain dialog renders paragraphs; run history's curated failure row (if the session has one) shows the prose reason.
5. **Minor gates:** default preference → no Refresh on the audit panel, no structural summary under a blob preview, no transcript in a recovery panel (trigger one only if a recovery error is reproducible; otherwise rely on the unit pin); flag on → all three present.
6. **Catalog:** every card's audit chips render; no grey "unknown" chip anywhere (none expected — the parity test is green).
7. **Keyboard path:** Tab from the Graph tab into the component list; the panel reveals; Enter opens "… configuration" with focus in it. (The e2e proves it on the Playwright backend; this confirms the deployed build.)
8. **Closed-`<details>` `aria-describedby` (Wave 2 deferral) — RESOLVED STATICALLY; the manual check as originally written was unperformable and is replaced.** A pre-fix draft asked the operator to "focus a SchemaFormTurn field whose description sits inside a CLOSED 'Advanced settings' `<details>`". **That configuration does not exist.** In all seven of `SchemaFormTurn.tsx`'s field renderers the `<p id={descriptionId}>` is a SIBLING of its own control, a few lines below it — `:537`/`:560`, `:566`/`:600`, `:620`/`:640`, `:646`/`:662`, `:676`/`:692`, `:703`/`:725`, `:740`/… — so a control and its description are always in the same subtree. When `<details className="guided-schema-advanced" open={showAdvanced}>` (`:207`, `:230`) is closed, the input is hidden **with** its description and cannot be focused at all. There is no state in which a focusable control's `aria-describedby` resolves into a closed `<details>`. The same holds for the two other candidates: `AcknowledgementCard.tsx`'s target `<p id={promptGateId}>` is at `:619` while `<details className="ack-card-original-template">` spans `:579-583`, a different subtree; `ToolCallCard.tsx`'s `<span id={describedById}>` is at `:36` beside its trigger at `:31`, while `<details className="tool-call-details">` is at `:188` in a different region.

   **So there is nothing to test manually, and an operator who tried would be unable to reproduce the setup and might file a spurious ticket off "inconclusive".** Record the structural finding in the epic comment — it is the answer the Wave 2 deferral was waiting for — and, if a screen reader is to hand at all, spend five minutes on the useful version instead: expand "Advanced settings" and confirm the description IS announced normally. No ticket unless that fails.
9. **Tutorial canary (ADR-031), with a stated pass criterion and the seam it is checking.** A pre-fix draft was nine words with no expectation, and the plan never established what exposure the canary was for. Traced: the guided lane builds its prompts from per-step skills (`guided/prompts.py:91-94`) and does **not** load `pipeline_composer.md`, so Task 6 cannot touch the nine guided steps. The one seam is the guided→freeform **graduation**: `guided/prompts.py:97` `build_mode_transition_system_prompt(*, terminal_reason, freeform_skill)` takes the fully processed freeform brief — including Task 6's new `## Reply Register` section and the new Termination States checklist line.

   Run the tutorial once end to end. **Pass criteria:** (a) the nine guided steps are byte-unaffected — same prompts, same turn shapes, no new or reordered step (they load per-step skills, not `pipeline_composer.md`, so any difference here is a real finding and not an expected consequence of Task 6); (b) graduation completes; (c) the **post-graduation freeform reply obeys the Reply Register** — steps by display label, no `is_valid:`/`options.`/enum tokens in prose. Record that graduation reply verbatim in the epic comment beside Check 3's. Like Check 3, (c) is a planner-behaviour observation rather than a pass/fail gate; (a) and (b) ARE gates — a difference there means Task 6 reached a path it should not have, which would be an ADR-031 violation and stops the closeout.

Report: one `filigree add-comment elspeth-cd8abcba3f` listing each check pass/fail with the frontend build id and the merged sha; a failed check REOPENS that task's ticket rather than being noted in prose.

- [ ] **Step 5: Ticket mechanics**

**The eight task-type tickets close with nothing extra.** `elspeth-93f5621f18`, `elspeth-d74ab492dd`, `elspeth-4bf65fe149`, `elspeth-d1feee1e67`, `elspeth-f1394307e3`, `elspeth-0bfd019f68`, `elspeth-13b69b5846`, `elspeth-59631ec7f7` are all type `task`, whose template has `open → closed` and `in_progress → closed` as direct SOFT transitions with **no required fields** (`filigree type-info task`) — so no `-f` flag is needed on any of them, from either state:

```bash
# Idempotent: skip any ticket already closed, so a second Task 11 pass is a
# no-op rather than eight failed transitions.
for id in elspeth-93f5621f18 elspeth-d74ab492dd elspeth-4bf65fe149 elspeth-d1feee1e67 \
          elspeth-f1394307e3 elspeth-0bfd019f68 elspeth-13b69b5846 elspeth-59631ec7f7; do
  # `filigree show` prints "Status:   <status>" on its own line (verified).
  if filigree show "$id" | grep -qE '^Status: +closed'; then
    echo "skip $id (already closed)"; continue
  fi
  filigree add-comment "$id" "<what landed, commit shas, what was verified (tests + live check number)>" --actor <assignee>
  filigree close "$id" --actor <assignee>
done
```

(The status line's shape was verified against a live `filigree show elspeth-93f5621f18`: `ID:`, `Title:`, `Status:`, `Priority:`, `Type:`, `Parent:`, `Created:`, `Labels:`, each label padded to a fixed column. `--json` is available on `show` if a future reader would rather parse than grep.)

**The bug is different — check its field BEFORE running the loop.** `elspeth-7d07df6438` should already be `verifying` with `fix_verification` set (Task 10 Step 3). Confirm it, because the close HARD-fails without it and there is no way to supply it on `filigree close`:

```bash
filigree show elspeth-7d07df6438           # expect: Status verifying, fix_verification present
filigree add-comment elspeth-7d07df6438 "<what landed, commit sha, tests + live check 5>" --actor <assignee>
filigree close elspeth-7d07df6438 --actor <assignee>
```

If `fix_verification` is missing (the lane closed out early, or the update was run without `-f`), set it first with `filigree update elspeth-7d07df6438 --actor <assignee> -f fix_verification="<how it was verified>"` and then close. Do NOT reach for `--force`: that uses the template's escape transition and records a close that skipped the workflow, which is worth avoiding for a one-flag fix.

Specifics: `elspeth-93f5621f18`'s comment records the "Removed" wording ruling and the three-state fallback, and names all three commits (Task 1, Task 3's topology extraction, and Task 4). `elspeth-d74ab492dd`'s comment records that its Spec-tab `<h4>`/kind/policy items landed in Task 4's commit, lists the eight register items with their commits, and the `"gpt"` acronym addition. `elspeth-59631ec7f7`'s comment states the one rule verbatim (Global Constraints) and the two commits applying it (Task 2 catalog row, Task 5 egress lines). `elspeth-4bf65fe149`'s comment records the test-module ruling (`test_prompts.py`, not `test_pipeline_planner.py`), the no-fixture ruling, and Check 3's verbatim reply. `elspeth-d1feee1e67`'s comment records the negative-control run and the ArrowDown parking. `elspeth-0bfd019f68` closes — **and its comment records the reframing from Task 9: the unknown-characteristic chip only ever rendered at `showAdvanced: true`, because an out-of-vocabulary flag is not in `DEFAULT_VISIBLE_AUDIT_FLAGS` and was filtered out before reaching the icon at the default level. This was a detailed-level cleanup, not a default-DOM fix; the real drift guard is and remains the Python↔TS parity test.** `elspeth-d1feee1e67`'s comment also records what the Task 7 spec does NOT pin: no assertion on the focused item's focus indicator (2.4.7) and none on focus-not-obscured (2.4.11) — write it as "the list's reveal-on-focus and the focus hand-off to the config panel are pinned", never as "keyboard access is pinned".

- [ ] **Step 5b: CREATE the Roadmap child tickets — BEFORE the epic-close decision**

**Every Roadmap row below marked "ticket" asserts a tracker row that no step created, and a pre-fix draft then let the epic close anyway.** That bites hardest on Task 10, whose honesty ruling rests on the park existing: *"parked with a ticket, not presented as clean"* is a claim about a row nobody filed. And the delivery posture is explicit that plan documents get "updated or deleted normally as the system changes" — so a decision recorded only here is a decision that disappears. Create these seven under the epic, then decide the epic's status:

```bash
# TITLE is POSITIONAL; the body flag is -d/--description. There is no --title
# and no --body (verified against `filigree create --help`).
filigree create "<title>" --type task --parent elspeth-cd8abcba3f --actor <assignee> \
  -d "<the Roadmap row's reason column, verbatim>"
```

1. `src/api/decodePrimitives.ts` — one leaf for the structural-decoder primitives, **scoped to all four copies** (`guidedDecoder.ts:102-190`, `api/auditReadiness.ts:39,:46`, `api/shareableReviews.ts:32,:50`, `api/preferencesDecoder.ts`) **and deciding the `ApiError`-vs-`Error` contract**.
2. Graph a11y list-item text in the reader register — `GraphView.tsx:1804` emits `` `${humanNodeType(typeLabel)}: ${id}${plugin ? ` (${plugin})` : ""} — …` ``. **Flag this one as the highest-value follow-up in the body:** that `<ol>` is the `role="img"` diagram's WCAG 1.1.1 text alternative, so it is the ONE surface where an AT user receives the identifier register exclusively, with no prose route at all.
3. Arrow-key navigation for the graph a11y list. **File it as an APG list-pattern / keyboard-effort enhancement, NOT as a WCAG compliance item** — every item is reachable by Tab (so not 2.1.1) and 2.4.1 Bypass Blocks concerns blocks repeated across pages (so not that either). The cost is a Tab run proportional to component count with no in-list skip.
4. `bind_source` option-value question.
5. Narrow `types/index.ts:180-181,:191` to the backend Literals.
6. **`ConfirmDialog`'s `aria-describedby` does not span its children** (`components/common/ConfirmDialog.tsx:72`, `messageId` → `:81-83` only; the egress `<ul>` arrives as `children` at `:84`). So the run-confirm dialog announces its message on open and nothing about what leaves. Parked out of Task 5 because `ConfirmDialog` is a shared primitive with consumers outside this wave.
7. **The two leverage items** — see the Roadmap rows: a default-DOM pin COVERAGE RATCHET, and a durable home for the epic's doctrine.

**Then, and only then:** the epic `elspeth-cd8abcba3f` closes with the wave summary. **Whichever way it goes, Step 4's epic comment must either link the durable-doctrine doc (row 7) or state plainly that the doctrine was deliberately not preserved** — one sentence in a comment that is being written anyway, so the decision is recorded either way instead of being an omission nobody notices.

---

## Roadmap: after Wave 3 (deliberately left out, with reasons)

| Item | Disposition | Why |
|---|---|---|
| `elspeth-062c1d0b7f` P3 — `tsc -p .` broken (non-composite references) | **Parked** | Fixing needs `composite: true` + `tsbuildinfo` emit interplay across three sub-projects, or deleting the root project; either changes every contributor's typecheck command. Own lane; every Wave 3 task uses the three explicit `-p` configs. |
| `elspeth-27e7cfeeee` P3 — lint script enumerates e2e specs | **Not absorbed** (Task 7 sidesteps it by naming the new spec to match the existing `composer-workspace-*` glob) | Linting `tests/e2e/**/*.spec.ts` wholesale may surface pre-existing findings in never-linted specs (`guided-collector.spec.ts` etc.); that is a cleanup lane, not a rider. |
| `elspeth-f5103c6706` P4 — run-time node labels on the diagnostics payload | **Parked** | Backend payload change; the curated row's `<code>{nodeId}` fallback is honest and pin-exempt. |
| `elspeth-f91cef2351` P4 — version tree `expandedGroups` pruning, Home/End keys, range-label contiguity | **Parked** | Wave 2 Task 7 minors; no register or detail-level content. |
| `elspeth-71bbf7eb12` P1 — tall-dialog `/validate` timeouts | **Not Wave 3 scope** (noted only so Task 7/11 do not depend on that fixture) | Separate P1 lane. |
| Backend `source` field on `CompositionStateVersion` for version edit-source labels | **Parked, not wanted now** | `deriveVersionLabel` already derives "Applied: <sentence>" from the tool messages (Wave 2 Task 1/7); a persisted field would duplicate a derivable fact and widen the session schema (epoch bump). Revisit only if a version ever has NO tool message to derive from. |
| Field names inside prompt-template excerpts (`case_study1`) | **Ruling: verbatim, not a defect** | A prompt template is authored content — the user's or the composer's words as the model receives them; rewriting a field reference inside it would misrepresent the prompt under review. Excerpt surfaces are exempted in pins via `allowSelectors` on the excerpt element, never by editing the text. |
| `src/api/decodePrimitives.ts` — one leaf module for the structural-decoder primitives, error prefix injected, scoped to **all four** existing copies | **Parked with a ticket — Task 11 Step 5 CREATES it; named as a known duplication, not presented as clean** | `preferencesDecoder.ts` (Task 10) copies `guidedDecoder.ts:102-190`'s private primitives — the same archetype as the Task 3 topology defect, caught by the structural sweep. Not fixed in Wave 3 because the primitives are called 487 times inside a 2289-line validation module and `invalid` hardcodes the "guided response" prefix in its body, so the extraction is a factory-or-thread-a-prefix refactor of the decoder, not a move. Exporting them unchanged would be worse than the copy — the preferences decoder would report "Invalid guided response at show_advanced". **The ticket must cover all four copies, not just Task 10's:** `guidedDecoder.ts:102-190`, `api/auditReadiness.ts:39,:46` (`unexpectedShape`/`isRecord`), `api/shareableReviews.ts:32,:50` (same two names, different signature), and `preferencesDecoder.ts`. **And it must DECIDE the error contract** — the two `api/` copies throw `ApiError`, the two decoders throw `Error` — otherwise a unification closes two of four and leaves the split it was written to remove. The fix is a leaf module with `prefix` as a parameter and all four importing it. |
| Narrow `types/index.ts:180-181,191` (`policy`, `merge`, `scope_policy`) from `string \| null` to the backend Literals | **Recommended follow-up, not a Wave 3 blocker** | Task 4 closes the `policy`/`merge` PHRASE maps against `lib/graphTopology`'s member tuples and `output_mode` against the union `types/index.ts:183` already declares, which gets the compile-time guard **against an unphrased member of those tuples** for one line and no other file touched; the separate backend→frontend drift guard is Task 3 Step 2b's parity assertion, not the closure (a pre-fix draft of this row asserted the closure caught backend additions — it does not, and the corrected statement is in Task 4's phrase-map ruling). Narrowing the wire types themselves is the fuller fix — it would delete the runtime `titleCaseLabel` fallback as unreachable — but it also touches `guidedDecoder.ts` and every composition fixture, so it is its own lane. `scope_policy` needs a backend Literal to exist first: there is none today. |
| `bind_source` as a reader-register phrase (named beside `require_all` in the `elspeth-d74ab492dd` roadmap row) | **Parked with a ticket — Task 11 Step 5b files it; the row's premise was wrong** | The row lists `bind_source` as a policy enum to phrase alongside `require_all`. It is not one. `bind_source` has ZERO hits in the frontend and is a backend OPTION VALUE — `options["mode"] == "bind_source"` (`web/composer/yaml_generator.py:94-95`, `pipeline_proposal.py:568`) — so it never reaches `PipelineSpecView`'s routing projection at all; it renders, if anywhere, through `OptionRows`, which this wave does not touch and which Wave 1 ruled renders user-keyed option content verbatim. Phrasing it means deciding whether option VALUES join the register rule, which is a wider decision than one enum and belongs in its own ticket. The `require_all` half of that row IS closed (Task 4's `POLICY_PHRASES`/`SCOPE_POLICY_PHRASES`). File the option-value question under the epic. |
| Graph a11y list item text `transform: classify (llm_transform)` (`GraphView.tsx:1804`) | **Observed, new ticket — Task 11 Step 5b files it. HIGHEST-VALUE follow-up on this list.** | `GraphView.tsx:1804` is the a11y label template `` `${humanNodeType(typeLabel)}: ${id}${plugin ? ` (${plugin})` : ""} — ${validity}…` `` — node id plus raw plugin id. The reader-register form is `<step label> (<plugin display name>)`. **Why it ranks first: that `<ol>` is the `role="img"` diagram's WCAG 1.1.1 text alternative, so it is the ONE surface in the composer where an assistive-technology user receives the identifier register EXCLUSIVELY** — every other surface this wave touches gives an AT user the prose and keeps the identifier in a `title`, a `data-*` or a `<code>`. Out of Wave 3's six rows; file as a register ticket under the epic. |
| Arrow-key (roving tabindex) navigation in the graph a11y list | **New enhancement ticket — Task 11 Step 5b files it. File it as an APG/usability enhancement, NOT a WCAG compliance item.** | The `elspeth-d1feee1e67` text assumed ArrowDown works; it does not — `GraphView.tsx:1893-1900` renders a plain `Button variant="bare"` per item with no `tabIndex` management. A roving-tabindex list is a parity change across the a11y suite; Task 7 pins Tab/Enter, which is what exists. **The cost is a Tab run proportional to component count with no in-list skip: an APG list-pattern deviation and a keyboard-effort burden. It is NOT a 2.4.1 Bypass Blocks failure** (2.4.1 concerns blocks repeated across pages) **and NOT a 2.1.1 failure** (every item is reachable). Naming that in the ticket keeps it from being argued as a compliance item it is not. |
| Closed-`<details>` aria-describedby AT behaviour | **RESOLVED statically; no manual check owed** (Task 11 Step 4.8 records the finding) | Cannot be pinned in jsdom, and does not need to be — **the question is answered, not deferred:** no `aria-describedby` anywhere resolves INTO a closed `<details>` subtree. `SchemaFormTurn.tsx` — the candidate a first sweep missed — puts every `<p id={descriptionId}>` as a SIBLING of its own control (`:537`/`:560` and six more pairs), so a closed `<details>` (`:207`, `:230`) hides the input together with its description and the control cannot be focused at all. `AcknowledgementCard.tsx` — the `aria-describedby` target `<p id={promptGateId}>` is at `:619`, and `<details className="ack-card-original-template">` spans `:579-583`: different subtree. `ToolCallCard.tsx` — the target `<span id={describedById}>` is at `:36`, beside its trigger at `:31`; `<details className="tool-call-details">` is at `:188`, a different region. So this park is a correct deferral of an unknown, not a deferred defect. |
| `ConfirmDialog`'s `aria-describedby` does not span its children | **New ticket — Task 11 Step 5b files it** | `components/common/ConfirmDialog.tsx:72` sets `aria-describedby={messageId}` pointing at the `<p className="confirm-dialog-message">` only (`:81-83`); the run-confirm egress `<ul>` arrives as `children` at `:84`, outside the description. So AT announces "Run pipeline" plus "This run leaves the composer and uses your stored credentials:" on open, and nothing about *what* leaves. Task 5's `.sr-only` + `aria-describedby` per line fixes the register problem (the identifier sentence is reachable once inside the list) but not the announcement-on-open problem. Parked out of Task 5 because `ConfirmDialog` is a shared primitive with consumers outside this wave and extending its description to span message-plus-children changes all of them. |
| **Default-DOM pin COVERAGE ratchet** — `src/test/defaultDomPinCoverage.test.ts` | **New ticket under the epic — Task 11 Step 5b files it. NOT Wave 3 (wave-sized on its own).** | The pin this whole epic rests on is an **undiscoverable opt-in**. Measured at the current tree: 110 files under `components/**/*.test.tsx`, **88** of which mount a component with `render(<`, of which **7** call the pin (9 files call it in total, counting two that render through a local helper) — under 10% — and `git grep expectNoIdentifiersInDefaultDom -- '*.md'` returns **0**. Nothing connects "this component renders prose" to "its test calls the pin"; a component added next month is uncovered by default and the failure mode is exactly this epic's defect. Three waves have each paid the tax as a hand-maintained coverage paragraph in a plan document (this plan's own "Which tasks carry the pin" enumeration IS that tax). **The general fix is the shape the repo already trusts:** `src/styles/classNames.test.ts` walks every TSX from disk and holds an allowlist where *"every entry states WHY the element needs no rule. 'It looks fine' is not a reason"* (`:29-31`). The analogue walks `components/**/*.test.tsx` and requires every file containing `render(` to import the pin or appear in `PIN_EXEMPT` with a one-line reason. **File it as a RATCHET, not a big-bang gate:** ~60 lines plus a generated allowlist seeding all current files as `"pre-gate, not adjudicated"`, so new files are gated from day one and the allowlist only shrinks. A big-bang version is ~101 files to adjudicate and would light up dozens of never-pinned surfaces mid-wave. **The cheap half — the USAGE CONVENTION — is taken in Task 2**, whose header edit now states "Every component test that renders user-facing prose calls this helper" (Step 2, line 4 of the header block). A first fix round banked this before it was true: that round's header edit added only the option descriptions and a pointer to the helper's own test, all of which help someone **already editing `defaultDomPins.ts`** — precisely the population that is not the problem, since reading the header requires already knowing the helper exists. The convention sentence is what a test author who has never seen the file needs, and it is now there. **The enforcing 20% — the ratchet gate — is this ticket and is NOT in Wave 3.** |
| **A durable home for the epic's doctrine** — `src/elspeth/web/frontend/AGENTS.md` or an ADR | **New ticket under the epic — Task 11 Step 5b files it; and Step 5's closing comment must link it or record that the doctrine was deliberately NOT preserved** | Three waves of doctrine live **only** inside plan documents. Measured: `git grep -ln show_advanced -- 'docs/**'` returns exactly the three `docs/plans/2026-08-*-composer-detail-level-wave{1,2,3}.md` files and nothing else; `CONTRIBUTING.md`, `AGENTS.md` and `docs/architecture/adr/` have zero hits for the register rule. AGENTS.md § delivery posture is explicit that plans "get updated or deleted normally as the system changes" — **so this doctrine is stored in the one class of document the project has committed to deleting.** Contents, at minimum: the two registers and the rule for choosing between them; the ~20-item **audit-required-stays-visible list**; "omit, don't hint"; `useShowAdvanced()` as the sole reader; the `<details>` uncontrolled idiom; a pointer to `expectNoIdentifiersInDefaultDom` as the executable form (which closes the discoverability half of the ratchet row for free); the placement convention that a rule mirrored from a backend authority lives in `src/lib/` with a header naming the authority by path; **and an INVENTORY TABLE of every `show_advanced` surface — gated, or deliberately not, with the reason.** That table is the missing completion criterion: 11 components read `useShowAdvanced()` today and Task 8 adds three, each picked as "the next few surfaces" with no record of what was adjudicated and left ungated — e.g. `HeaderSessionSwitcher`'s "Show archived", excluded because it is the only archive-restore path, a real ruling recorded nowhere durable. **Two homes fit the repo's conventions and the choice is a real one:** a directory-scoped `frontend/AGENTS.md` (the pattern root AGENTS.md already blesses by name for `examples/` and `plugins/transforms/`) is cheapest and sits beside the code; an ADR is stronger for the audit-required list specifically, because that list is a **product-integrity** artifact — what an auditor must never have hidden from them — which ADR-046 distinguishes from project-tooling ceremony. **Explicitly NOT a wrapper component or an `<AdvancedOnly>` HOC:** each gate is a distinct judgement about what plain summary stands in its place (RecoveryDiff; the blob preview; a refetch CADENCE, which is not visible content at all), and that judgement cannot be mechanised. A list, not a gate. |

**Sequencing rule (all waves):** one PR per ticket; each PR's default-DOM regression pin (`expectNoIdentifiersInDefaultDom`) is the acceptance test the reviewer runs first.
