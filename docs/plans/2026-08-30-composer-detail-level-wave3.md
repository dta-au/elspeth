# Composer Detail Level (Wave 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the epic's register residue and cleanup rows — the step-label fallback reversal and the Spec tab's remaining raw ids, the eight-item register batch, the freeform brief's reader-register rule, the graph-list keyboard e2e, three minor `show_advanced` gates, and the dead unknown-characteristic chip — plus the small mechanism and hygiene items Wave 2 deferred.

**Architecture:** No new mechanisms and no new backend surface. Every visible identifier goes through an existing single-authority helper — `stepLabelForNodeId` / `humaniseStepLabel` (`chat/interpretationStepLabel.ts`), `pluginDisplayName` / `titleCaseLabel` (`catalog/pluginDisplayName.ts`), `makePhraseFor` (`lib/validationHumaniser.ts`) — with the raw id demoted to `title`. Connection topology is likewise a single authority, and it already exists: `inspector/GraphView.tsx` carries the backend-mirrored rules (`publishedSuccessConnection`, `branchEntries`, the fan-in and implicit-publisher kind sets) as module-private helpers. Task 3 LIFTS them into `lib/graphTopology.ts` unchanged — a LEAF module in the repo's own home for one-rule-one-place helpers (`lib/validationHumaniser.ts`, and `chat/guided/stepLabels.ts`, which exists because "three hand-mirrored STEP_LABELS copies had drifted") — together with the two coalesce member sets `api/guidedDecoder.ts` held privately and the `discard` sentinel three sites spelled by hand. Task 4's new `workspace/specRouting.ts` builds on all of it, so the Spec tab can say "Then → Extract Invoice" instead of `raw_rows` without re-deriving a rival model of the graph. Gated surfaces read `useShowAdvanced()` and simply omit the technical control with the flag off (the Wave 2 `CompletionBar` precedent: the sibling content IS the plain summary; no "available at the detailed level" hints). The planner-brief change is skill text only (Composer invariant 1; ADR-031). The shared default-DOM pin helper gains an `allowAriaLabelSelectors` option so surfaces whose accessible names carry author-chosen ids by design can join the gate.

**Tech Stack:** React 18 + Zustand + vitest/@testing-library + Playwright (frontend, `src/elspeth/web/frontend`), pytest (backend, `tests/unit/web/composer`), FastAPI (unchanged).

**Spec:** Wave 2 plan §"Roadmap: Wave 3" (`docs/plans/2026-08-30-composer-detail-level-wave2.md:2299-2316`) — the six-row table is the binding scope; the Wave 1 live-check report (`.superpowers/sdd/2026-08-29-composer-detail-level-wave1/live-check-report.md` §Check 1, 83 snake_case text nodes on session 39578c6f at the default preference) is the Spec-tab acceptance baseline; epic `elspeth-cd8abcba3f`; tickets `elspeth-93f5621f18`, `elspeth-d74ab492dd`, `elspeth-4bf65fe149`, `elspeth-d1feee1e67`, `elspeth-f1394307e3`, `elspeth-0bfd019f68`; absorbed follow-ups `elspeth-13b69b5846`, `elspeth-59631ec7f7`, `elspeth-7d07df6438`. Every `file:line` below was re-verified against branch head `cde8a279b` (2026-08-30) — see the drift record immediately below for the current base and why `cde8a279b`'s citations still hold against it; the ticket bodies carry 2026-08-28 line numbers that have drifted and are NOT authoritative.

**Base-commit drift (recorded at the review-fix pass, 2026-08-30, and re-checked at the end of it — the branch moved twice while the pass ran):** the plan's citations were verified at `cde8a279b`; branch HEAD is now **`8392d0113`**. `cde8a279b` is an ancestor. The intervening commits, all checked against this plan's citation set:

- `dffa61b7a`, `cac5a8ccb`, `7a12fe3f0` — docs-only (a tier-burndown sweep restore, and the design + plan for the composer-preferences OK action). No source files.
- `0411c438c` `feat(web): add Composer preferences OK action` — `settings/ComposerPreferencesPanel.tsx`, `settings/ComposerPreferencesPanel.test.tsx`, `settings/settings.css`, `settings/settingsSurface.test.ts`.
- `8392d0113` `feat(lints): check-judge-quality can measure the judge that actually signs` — `elspeth-lints/…/cli.py`, `judge_quality.py`, `tests/unit/elspeth_lints/test_judge_quality.py`.

**No file this plan cites appears in either source commit.** Checked mechanically: the only mentions of those four `settings/` files anywhere in this document are in the paragraph below, which names them precisely to record that Wave 3 does NOT touch them. So every `file:line` below still holds at `8392d0113`.

Two consequences worth stating rather than leaving implicit. (a) The composer-preferences OK lane has **landed**, so it is no longer a concurrent lane to coordinate with — it is part of the base. (b) `8392d0113` changes `elspeth-lints` CLI code, which is the tool Task 0 Step 1 runs. That is precisely why Task 0 re-captures the corpus at the real branch point instead of trusting the Wave 2 close figure of 2354 — a different count here is the tool moving, not a finding. Re-run Task 0 Step 1 at whatever HEAD the wave actually branches from; that capture, not any number written here, is the authority for Task 11's diff.

**Sibling lane, for the record (now merged, previously concurrent):** `docs/superpowers/plans/2026-08-30-composer-preferences-ok-button.md` touched `settings/ComposerPreferencesPanel.tsx`, `settings/settings.css`, `settings/ComposerPreferencesPanel.test.tsx` and `settings/settingsSurface.test.ts`. Wave 3's only `settings/` file is `SecretsPanel.tsx` (Task 5 Step 2), and it adds no class name and no CSS rule — so there was zero file overlap and there is no merge interaction to expect. If that lane is ever re-run or extended, the same independence holds.

## Global Constraints

- **Read `CONTRIBUTING.md` §"Whole-tree gates and conventions you will hit" before touching code.** No new `getattr`/`hasattr` anywhere (attribute-contracts + masquerade gates scan the whole tree, tests included). Owned types get direct attribute access; parse only genuine Tier-3 boundaries via the file's existing idioms (per ADR-032). No task in this wave adds a Python probe; Task 6 is the only backend-touching task and it edits a Markdown skill file plus one Python test.
- **Trust-tier lint corpus is fail-closed and must not grow.** Task 0 captures the before-corpus at the branch point, before any lane lands; Task 11 diffs the after-capture against it; the diff must add nothing. Capture is **stdout only** (`> file`, never `2>&1` — stderr adds 5 WARNING lines and corrupts the count). Baseline at 37e939bc3 was 2354; re-capture at the Wave 3 branch point rather than trusting that number. Never hand-edit a `judge_metadata_signature`; never shape code around signature churn.
- **Composer invariants:** no server-side authoring of pipeline structure; **no tutorial-special paths** (ADR-031). Task 6 changes the brief only — no rewriting, filtering, or scoring of model output anywhere on the server, and no tutorial-conditional prose.
- **Audit-required elements stay visible regardless of `show_advanced`:** AuthorityChip, Audit panel rows + Blocks-run/Advisory legend, Run-confirm egress lines (every sentence; R2-F7 must not reappear), tool-outcome ribbon prefixes (Applied / Looked up / Completed / Ran / Attempted (not applied) / Failed / Cancelled), acknowledgement cards, completion honesty gate, "Validation passed · N checks" headline, the accounting-corruption badge, the audit-closure verdict line and missing/duplicate-terminal integrity warnings, the run history Cancel affordance, the wire-stage blocker panel, every version remaining revertable, the curated per-state failure row in run history (`RunStateFailureDetail`), RecoveryDiff + Discard/Apply in the recovery panel, and the blob row's status dot, creator badge and four actions. This wave changes the WORDS on several of these; it never hides one.
- **Debug mode expands disclosures; it never adds surfaces.** Every item hidden when the flag is off has a plain summary in its place. For Task 8 the plain summary is the sibling content that already carries the fact (the diff, the preview, the auto-refreshing panel) — no hint text pointing at the preference.
- **`<details open={showAdvanced}>` is uncontrolled after the first user toggle** (Wave 1 idiom, `OptionRows.tsx:201`). No task makes a `<details>` controlled.
- **Every flag reader goes through `useShowAdvanced()`** (`@/stores/preferencesStore`); the preferences panel is the only direct store reader.
- **No task regenerates goldens or touches `src/elspeth/plugins/*`.** If `tests/golden/web/catalog/knob_schema/*.json` or `docs/architecture/dag/scenario-corpus/v1/manifest.yaml` shows dirty, stop: something out of scope changed.
- **New TSX class names need real stylesheet rules** — `src/styles/classNames.test.ts` is a whole-tree gate; the directory gates (`catalogClassNames.test.ts`, `executionClassNames.test.ts`) also assert their `RULE_LESS_BY_DESIGN` names are still applied somewhere. Task 9 REMOVES a rule-less name and must remove its allowlist entry in the same commit or the "keeps the rule-less allowlist honest" test fails.
- **Test files touching a Zustand store must reset it** — a top-level `beforeEach(() => resetStore(useXStore))` (from `@/test/store-helpers`) in every test file whose component becomes a flag reader in this wave. The files this wave turns into readers: `RecoveryPanel.test.tsx`, `BlobRow.test.tsx`, `AuditReadinessPanel.test.tsx` (Task 8). The reset is a numbered step, not a "verify".
- **Copy register:** sentence case, no internal identifiers in visible text; raw identifiers go in `title`/`data-*` or a `<code>`/mono secondary span. A `<details>` is NOT a firewall for regex-based DOM pins — its children still land in the text scan.
- **The per-PR default-DOM acceptance pin is ONE helper, used by every task that renders DOM:** `expectNoIdentifiersInDefaultDom(container, { allowSelectors?, allowAriaLabelSelectors? })` from `src/test/defaultDomPins.ts`. It joins text NODES (not `textContent`; fixed 56065e665) and, after Task 2, exempts the aria-labels of elements matching `allowAriaLabelSelectors` — before Task 2 it has NO aria-label exemption beyond the ToolCallInfo trigger. `title` attributes are never inspected. The reviewer runs these tests first.

  **Which tasks carry the pin, and the two honest exemptions.** Tasks 1, 2, 4, 5, 8 and 9 each add at least one call (Task 8 adds three — one per gated component). The exemptions, stated rather than left as a silent gap:
  - **Task 3 (topology extraction) renders no DOM.** It is a pure relocation of module-private symbols out of two files plus a unit test; its acceptance is **both** `GraphView.test.tsx` and `guidedDecoder.test.ts` staying green and untouched, with a `git diff` on each showing pure relocation. A DOM pin there would have nothing to render.
  - **Task 9's own component becomes empty**, so `expectNoIdentifiersInDefaultDom` on `AuditCharacteristicIcon`'s container would be a check that cannot fail. The pin therefore goes one level up, on `PluginCard` (`AuditCharacteristicIcon`'s only consumer, `PluginCard.tsx:199`), where a raw backend flag would actually have reached visible text.
  - Tasks 6 (planner brief, Markdown + pytest), 7 (Playwright e2e) and 10 (API decoder, no DOM) render no React DOM at all; their acceptance is the pytest pin, the Playwright assertions and the decoder unit tests respectively.
- **The one rule for author-chosen ids (ruling for `elspeth-59631ec7f7`, applied in Tasks 1, 4, 5):** in PROSE surfaces (acknowledgement cards, wire blockers, execution/run history, run-confirm egress lines, Spec-tab headings and routing) an author-chosen component id renders through `stepLabelForNodeId`/`makePhraseFor` — description → acronym-aware title case of the id → plugin gloss — with the raw id in `title`. In IDENTIFIER surfaces (the catalog/import "unavailable component" row, guided component-review rows, `<code>` fallbacks) the id IS the actionable name the user must match against their own YAML or guided input and renders raw, in the identifier register (`<code>`), never bare prose. Cost if wrong: a surface classed wrongly shows a title-cased name where a copyable id was needed (recoverable via `title`) or vice versa (a snake_case token in prose, caught by the pin).
- **Shared checkout:** stage only your own pathspecs; never `git restore`/`clean` files you did not stage; no `git stash` (hook-blocked). **This forbids in-place mutation of a tracked file as a test technique, including a `cp` backup/restore round-trip** — a sibling lane's uncommitted work sits in this tree, and any window in which a tracked file holds content nobody staged is the same hazard class as `git restore`, only narrower. Task 7's negative control therefore injects its override at runtime (Playwright `addStyleTag`), never by editing the stylesheet on disk. Full `pytest tests/` is a background job in a worktree — cap parallelism at `-n 12` when other agents are running. Never run two Playwright commands concurrently in one worktree (auth state is worktree-global); Playwright boots its own backend on :8451 (`playwright.config.ts:19`).
- Frontend commands run from `src/elspeth/web/frontend`: `npx vitest run <path>`; `npm run lint` (`package.json:17` — eslint over `src` plus the enumerated e2e globs; `tests/e2e/composer-workspace-*.spec.ts` IS in the list, which is why Task 7's spec is named to match it); typecheck with `npx tsc --noEmit -p tsconfig.app.json && npx tsc --noEmit -p tsconfig.test.json && npx tsc --noEmit -p tsconfig.e2e.json` — `npx tsc --noEmit -p .` is broken (TS6305, `elspeth-062c1d0b7f`, parked). Backend from repo root with `source .venv/bin/activate`.
- **Sequencing rule:** one PR per ticket; each PR's default-DOM regression pin (the shared helper above) is the acceptance test the reviewer runs first. Task order is mechanism-first (Tasks 1–2), then the shared connection-topology extraction and the Spec-tab residue it unblocks (Tasks 3–4, the largest visible win), then the register batch (Task 5), then the brief, e2e, minors, cleanup, and the absorbed decoder bug (Tasks 6–10).

  **The real dependency graph is `2 → 3 → 4` and `2 → 8`; Tasks 5, 6, 7, 9 and 10 are independent and may all run in parallel.** Task 4 consumes Task 3's `graphTopology.ts` and Task 2's `allowAriaLabelSelectors`; Task 8 consumes Task 2's pin option. Task 5 (register batch) has NO Spec-tab half — Task 4's own ruling pulls the `<h4>`/`Kind`/policy-enum items of `elspeth-d74ab492dd` into Task 4's file and PR — so Task 5 must NOT be serialised behind Tasks 3–4. (A pre-fix draft of this line claimed "Task 4's Spec-tab half depends on Task 3"; it was wrong in both halves and is corrected here.)
- **Ticket mechanics.** Tickets assigned to a lane identity must be closed with `--actor <assignee>`.
  - **Tasks (eight of the nine):** `open → closed` is a direct SOFT transition with no required fields (`filigree type-info task`), so `filigree close <id> --actor <lane>` works from `open` or `in_progress` with nothing else to supply.
  - **The bug (`elspeth-7d07df6438`, type `bug`, status `triage`) is not the same shape, and one of its transitions HARD-fails.** `filigree type-info bug` declares `verifying → closed [hard] (requires: fix_verification)`, and `filigree close` has **no** `--field`/`-f` option to supply it (`filigree close --help`: only `--reason --status --force --expected-assignee --commit --json --actor`). A close attempt without the field aborts with `Cannot transition 'verifying' -> 'closed' for type 'bug': missing required fields: fix_verification`. Two further fields are declared required at earlier states — `severity` at `confirmed`, `root_cause` at `fixing` — but those transitions are SOFT, so they warn rather than fail.

    **Ruling — set all three fields at the transition that declares them, not just the one that hard-fails.** The hard gate is `fix_verification`, but leaving `severity` and `root_cause` unset ships a ticket whose own template says they are required, and the warning is exactly the kind of noise that trains a reader to ignore warnings. `filigree update` takes `-f key=value` (repeatable), so each is one flag on a command the plan already runs. Cost if wrong: three extra flags on two commands. The concrete sequence is in Task 0 Step 2 (claim) and Task 10 Step 3 (verify).

---

## File Structure

| File | Responsibility |
|---|---|
| `src/elspeth/web/frontend/src/components/chat/interpretationStepLabel.ts` (modify) | `humaniseStepLabel` reversal: present-but-unlabelable → title-cased author name; absent from a loaded composition → "Removed"; unloaded composition → title-cased id; new `isComponentPresent` |
| `src/elspeth/web/frontend/src/components/chat/AcknowledgementCard.tsx` (modify) | `data-affected-node-id` on the card `<section>` (forensic home for the raw id) |
| `src/elspeth/web/frontend/src/components/execution/ValidationResult.tsx` (modify) | the `!nodes` raw-id fallback → `phraseFor` |
| `src/elspeth/web/frontend/src/test/defaultDomPins.ts` (modify) | `allowAriaLabelSelectors` option |
| `src/elspeth/web/frontend/src/components/catalog/UnavailableComponentRow.tsx` (modify) | component id in `<code>` (identifier register by design) |
| `src/elspeth/web/frontend/src/stores/preferencesStore.test.ts` (modify) | move the tutorial-resume test into its describe block (Wave 1 deferral) |
| `src/elspeth/web/frontend/src/lib/graphTopology.ts` (create) | THE frontend connection-topology model — a LEAF module beside `lib/validationHumaniser.ts`. Lifted verbatim out of `GraphView.tsx`: `publishedSuccessConnection`, `branchEntries`, `IMPLICIT_SELF_PUBLISHING_NODE_TYPES`, `FAN_IN_NODE_TYPES`; lifted out of `api/guidedDecoder.ts`: `COALESCE_POLICIES`, `COALESCE_MERGES` (as union-typed tuples); plus the newly named `DISCARD_CONNECTION` sentinel |
| `src/elspeth/web/frontend/src/components/inspector/GraphView.tsx` (modify) | the four helpers above become imports; **no behaviour change** — `GraphView.test.tsx` is untouched and must stay green |
| `src/elspeth/web/frontend/src/api/guidedDecoder.ts` (modify) | `:75-76`'s two private coalesce member sets become imports; **no behaviour change** — `guidedDecoder.test.ts` is untouched and must stay green |
| `src/elspeth/web/frontend/src/components/workspace/specRouting.ts` (create) | pure: connection index (built on `graphTopology`), connection → component phrases, routing/policy enum phrases |
| `src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.tsx` (modify) | `<h4>` step label + `title`, `Kind` humanised, routing `<dd>` phrases with raw in `title`, policy enums |
| `src/elspeth/web/frontend/src/components/chat/modelDisplayName.ts` (create) | `modelDisplayName(modelId)` — leaf segment, hyphens to spaces, acronym-aware title case |
| `src/elspeth/web/frontend/src/components/catalog/pluginDisplayName.ts` (modify) | `"gpt"` joins the acronym set |
| `src/elspeth/web/frontend/src/components/chat/ModelChip.tsx` (modify) | display name visible, raw id in `title`, aria-label in the reader register |
| `src/elspeth/web/frontend/src/components/settings/SecretsPanel.tsx` (modify) | `ScopeBadge` label map (Yours / Deployment / Organisation), raw scope in `title` |
| `src/elspeth/web/frontend/src/components/chat/AcknowledgementCard.tsx` (modify) | amendment cap warning in characters, exact bytes in `title` |
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

```bash
source .venv/bin/activate
git rev-parse HEAD   # record beside the count
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth > /tmp/w3-lints-before.txt; grep -c . /tmp/w3-lints-before.txt  # grep -c prints 0 and exits 1 on zero matches — do not wrap in set -e; stdout ONLY (no 2>&1)
```

Record the count and the HEAD sha. The Wave 2 close figure was 2354 at 37e939bc3; a different number here is branch drift, not a finding — the before-capture is the authority for Task 11.

- [ ] **Step 2: Confirm the tracker state**

**Snapshot, not a finding — as of `cde8a279b` (2026-08-30), and the branch has moved since:** all six Wave 3 tickets were `open`, type `task`, `Ready: YES (no blockers)`, parent `elspeth-cd8abcba3f`. Treat every state below as stale until the re-check in the next paragraph confirms it; do NOT skip that re-check because this paragraph looks authoritative. The absorbed follow-ups: `elspeth-13b69b5846` (task, open, no parent), `elspeth-59631ec7f7` (task, open, no parent), `elspeth-7d07df6438` (**bug, triage**, no parent). The hub re-runs `filigree show <id>` on all nine; if any has been claimed or closed since, stop and re-plan that task. Lanes then claim with `filigree start-work <id> --assignee <lane>`. Optionally parent the three absorbed tickets to the epic (`filigree update <id> --parent elspeth-cd8abcba3f`) so the epic's child list is the wave's ledger.

The bug is claimed differently — `--advance` walks it `triage → confirmed → fixing`, and those two states declare required fields (Global Constraints § Ticket mechanics). Supply them in the same call:

```bash
filigree start-work elspeth-7d07df6438 --assignee <lane> --advance
filigree update elspeth-7d07df6438 --actor <lane> \
  -f severity=major \
  -f root_cause="api/client.ts parseResponse ends in an unchecked response.json() as T cast, so a preferences payload with show_advanced omitted loads undefined into preferencesStore and renders <details open={undefined}> closed by accident."
```

(`severity` is an enum over `critical|major|minor|cosmetic`, default `major`; a silent wrong default on every Wave 1/2 disclosure surface is `major`, not `minor`.) `fix_verification` is set later, at the `verifying` transition in Task 10 Step 3, because only then is there a verification to describe.

- [ ] **Step 3: Confirm the live-check baseline is retrievable**

`filigree show elspeth-cd8abcba3f` and read the Wave 1 epic comment that records the post-3b7281965 Spec-tab residual count for session 39578c6f. Task 11's Check 1 compares against that figure. If the comment is missing, record "no baseline" now — Task 11 then reports an absolute count, not a delta.

---

### Task 1: Step-label fallback reversal — no raw node id in prose (`elspeth-93f5621f18`, part A)

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/chat/interpretationStepLabel.ts` (doctrine comment `:120-124`, `stepLabelForNodeId` `:126-147`, `humaniseStepLabel` `:149-159`)
- Modify: `src/elspeth/web/frontend/src/components/chat/interpretationStepLabel.test.ts` (`:112-115` raw-id pin, `:250-254` fan_out pin)
- Modify: `src/elspeth/web/frontend/src/components/chat/AcknowledgementCard.tsx:597-604` (the card `<section>`)
- Modify: `src/elspeth/web/frontend/src/components/execution/ValidationResult.tsx:79` (`if (!nodes) return componentId;`)
- Test: `src/elspeth/web/frontend/src/components/chat/AcknowledgementStack.test.tsx`, `src/elspeth/web/frontend/src/components/execution/ValidationResult.test.tsx`

**Interfaces:**
- Produces: `humaniseStepLabel(state, nodeId): string` — never the raw id. `isComponentPresent(state, nodeId): boolean` (new export, used by `humaniseStepLabel` and, in Task 4, by `specRouting`'s `scope_opener` arm — see that task's ruling for why it is the ONLY routing field where "present vs absent" is a real distinction). `stepLabelForNodeId`'s null contract is UNCHANGED (ticket instruction; five callers depend on it: `ReadinessRowDetail.tsx:64`, `SideRailValidationBanner.tsx:199`, `ValidationResult.tsx:118`, `PipelineValidationSummary.tsx:78`, and the wire-blocker path).
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

`ValidationResult.tsx:79` — `if (!nodes) return componentId;` becomes `if (!nodes) return phraseFor(componentId);`. Then retarget the docblock. The sentence that this change makes FALSE is mid-docblock at `:57-59`, not the docblock's last sentence (`resolveComponentName`'s docblock runs `:52-65`; its last sentence, about the `phraseFor` fallback for a vanished node, is already true and stays):

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
    expect(screen.getByText(new RegExp(UNKNOWN_COMPONENT_PHRASE))).toBeInTheDocument();
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
- Modify: `src/elspeth/web/frontend/src/test/defaultDomPins.ts` (`options` type `:37-40`, aria loop `:49-56`)
- Modify: `src/elspeth/web/frontend/src/components/catalog/UnavailableComponentRow.tsx:45` (`<strong>{finding.component_id}</strong>`)
- Modify: `src/elspeth/web/frontend/src/components/catalog/CatalogDrawer.test.tsx` (add the pin)
- Modify: `src/elspeth/web/frontend/src/stores/preferencesStore.test.ts` (`:397-434` moves under `:868`)

**Interfaces:**
- Produces: `expectNoIdentifiersInDefaultDom(container, { allowSelectors?: readonly string[]; allowAriaLabelSelectors?: readonly string[] })`. Elements matching `allowAriaLabelSelectors` (and their descendants) are skipped by the aria-label loop only; the text scan is unaffected. Task 4 consumes it for `.pipeline-spec-card`; this task consumes it for `.import-yaml-actions`.

**Ruling — the unavailable row's component id is an identifier surface:** the Remove/Replace buttons name the component by id in their aria-labels by design (`CatalogDrawer.tsx:575,583`), because the id is what the user must match against the YAML they imported. Per the one-rule constraint it renders in `<code>` (the pin's identifier surface) instead of bare `<strong>`; the plugin is already a display name with the raw id in `title`. This closes the catalog/import half of `elspeth-59631ec7f7`. Cost if wrong: a `<code>` where a bold name was wanted — a one-line revert.

- [ ] **Step 1: Extend the helper**

`defaultDomPins.ts` — replace the signature and the aria loop:

```ts
export function expectNoIdentifiersInDefaultDom(
  container: HTMLElement,
  options: {
    allowSelectors?: readonly string[];
    /** Elements (and descendants) whose accessible NAME carries an
     *  author-chosen id by design — the Spec-tab card ("Node rows"), the
     *  catalog's unavailable-component buttons. Exempts the aria-label loop
     *  only; visible text is still scanned (elspeth-13b69b5846). */
    allowAriaLabelSelectors?: readonly string[];
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
  const ariaExempt = options.allowAriaLabelSelectors ?? [];
  for (const el of container.querySelectorAll("[aria-label]")) {
    const label = el.getAttribute("aria-label") ?? "";
    if (/^What does .* do\?$/.test(label)) continue; // ToolCallInfo trigger
    if (ariaExempt.some((selector) => el.closest(selector) !== null)) continue;
    expect(label).not.toMatch(UUID_RE);
    expect(label).not.toMatch(HEX32_RE);
    expect(label).not.toMatch(SNAKE_RE);
  }
}
```

Update the header comment (`:1-10`) to mention the new option in one line: "`allowAriaLabelSelectors` exempts accessible names that carry an author-chosen id by design."

- [ ] **Step 2: The row, and a pin on both mounts**

`UnavailableComponentRow.tsx:45` — `<strong>{finding.component_id}</strong>` → `<strong><code>{finding.component_id}</code></strong>`; extend the header comment (`:1-8`): "The authored component id is the actionable name and stays — in `<code>`, the identifier register, because the user matches it against their own YAML (elspeth-59631ec7f7 ruling)."

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

(Import `expectNoIdentifiersInDefaultDom` from `@/test/defaultDomPins`. The exemption selector is `.import-yaml-actions` — the `<div>` `UnavailableComponentRow` wraps around `actions`, and therefore the tightest `closest()` ancestor of the two aria-labelled Remove/Replace buttons — NOT the whole `.validation-banner-error-item` row, which would also exempt any future aria-label added to the row's own content.)

In `src/components/sidebar/ImportYamlModal.test.tsx` the existing pin sits on a YAML with no unavailable component (ticket note); add one test that feeds a preflight response carrying one such finding and calls the helper with the same exemption. **The field is `plugin_policy_findings`, not `disabled_components`** (which has zero hits in that file; the wire name is confirmed at `ImportYamlModal.tsx:880`, `result.plugin_policy_findings ?? []`). The directly reusable pattern is the existing `it("renders sanitized disabled-component repair actions without fetching private schema", …)` at `:1048`, whose `api.importCompositionYaml` mock resolves with a `plugin_policy_findings` array at `:1055` and which already drives the `UnavailableComponentRow` render path via `getByRole("region", { name: /unavailable saved components/i })`. Copy its mock and its await, then pin.

Run: `npx vitest run src/components/catalog src/components/sidebar/ImportYamlModal.test.tsx src/test` → PASS.

- [ ] **Step 3: preferencesStore describe move (Wave 1 deferral, test-only)**

`preferencesStore.test.ts`: the test `it("resetTutorial clears tutorial_completed_at through the PATCH contract", …)` (`:397-434`) exercises the four resume fields but sits in the top-level `describe("preferencesStore")`; the resume-state describe is `describe("preferencesStore — tutorial resume state (elspeth-918f4434b3)")` at `:868`, which has its own `beforeEach` (`resetStore` + `vi.clearAllMocks()`). Cut the test verbatim and paste it as the last `it` of that describe. No edits to its body. Run: `npx vitest run src/stores/preferencesStore.test.ts` → PASS with the same test count.

- [ ] **Step 4: Commit (two commits — the helper/row are a ticket, the move is hygiene)**

```bash
git add src/elspeth/web/frontend/src/test/defaultDomPins.ts src/elspeth/web/frontend/src/components/catalog/UnavailableComponentRow.tsx src/elspeth/web/frontend/src/components/catalog/CatalogDrawer.test.tsx src/elspeth/web/frontend/src/components/sidebar/ImportYamlModal.test.tsx
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
- Modify: `src/elspeth/web/frontend/src/components/inspector/GraphView.tsx` (`:86-181` — four helpers move out and come back as imports; `:1313`, `:1365-1367` call sites unchanged)
- Modify: `src/elspeth/web/frontend/src/api/guidedDecoder.ts` (`:75-76` — `COALESCE_POLICIES` / `COALESCE_MERGES` move out and come back as imports; every other line untouched)
- **Unchanged, must stay green:** `src/elspeth/web/frontend/src/components/inspector/GraphView.test.tsx`, `src/elspeth/web/frontend/src/api/guidedDecoder.test.ts`

**Interfaces:**
- Produces, all from `lib/graphTopology.ts`. Four are lifted VERBATIM from `GraphView.tsx`:
  - `IMPLICIT_SELF_PUBLISHING_NODE_TYPES: ReadonlySet<string>` — `queue`, `coalesce`, `aggregation` (`GraphView.tsx:136-140`)
  - `publishedSuccessConnection(node): string | null` (`:142-152`)
  - `FAN_IN_NODE_TYPES: ReadonlySet<string>` — `row_union`, `coalesce` (`:155-158`)
  - `branchEntries(branches): [string, string][]` (`:175-181`)
  Two more are lifted from `api/guidedDecoder.ts:75-76`, retyped as `as const` tuples so they carry a union type (see the member-set ruling):
  - `COALESCE_POLICIES` + `type CoalescePolicy`
  - `COALESCE_MERGES` + `type CoalesceMerge`
  And one is new, naming a sentinel that three sites currently spell as a bare literal:
  - `DISCARD_CONNECTION = "discard"`
- Consumes: nothing. Pure, no React, no store, no CSS — a LEAF module, the same contract `lib/validationHumaniser.ts:13-16` and `components/chat/guided/stepLabels.ts:5-7` state for themselves. Typed structurally against the fields it reads so both `NodeSpec` and the graph's local node shape satisfy it.
- **Blast radius, measured.** `git grep -n "publishedSuccessConnection\|branchEntries\|FAN_IN_NODE_TYPES\|IMPLICIT_SELF_PUBLISHING_NODE_TYPES" -- src tests` (run 2026-08-30) returns hits in `GraphView.tsx` ONLY — three definitions, three call sites, three comment mentions; nothing else in the tree, tests included. `COALESCE_POLICIES` / `COALESCE_MERGES` return hits in `guidedDecoder.ts` only. So this task modifies two production files and adds two; **no test file changes at all**, which is the point (see the test-placement ruling).

**Ruling — extract and reuse, do not re-derive.** The Spec tab (Task 4) needs to answer "which component is on the other end of this connection?". That question already has an answer in this codebase, in `GraphView.tsx`, mirrored from two named backend authorities, and it is *scarred*: `publishedSuccessConnection`'s comment records session 3f02c8fa (a working fork/coalesce pipeline drawn as two disconnected fragments because the rule was re-derived from `on_success` alone) and `branchEntries`' comment records `elspeth-625e85c59b` (a coalesce drawn with a single arm because `branches` was assumed to be a map). A second module re-deriving the same rules is a second place for those two incidents to recur, and the pre-fix draft of this plan did exactly that and inverted the direction of `branches` in the process. The authorities:

- `src/elspeth/core/config.py:984-986` — `CoalesceSpec.branches` is a **"Branch identity → input connection mapping"**. A fan-in node's `branches` are its INPUTS. Not its outputs.
- `src/elspeth/web/composer/guided/connection_consumers.py:31-40` — the canonical consumer projection. For `coalesce`/`row_union` it registers each BRANCH connection as consumed by the node and then `continue`s; it **never** registers `node.input` for a fan-in node, because that scalar is only the backend-compatible first-branch placeholder.
- `_producer_resolver.published_success_connection` — `on_success` if set; else the node id for `queue`/`coalesce`/`aggregation`; else nothing. `GraphView.tsx:112-135` already mirrors it and says so.

Cost if wrong: the Spec tab and the Graph tab disagree about the same pipeline, on the exact shape (a coalesce) where they have already disagreed once in production.

**Ruling — the LOCATION and the NAME are the fix, not packaging around it: `src/lib/graphTopology.ts`.** The failure mode is discoverability, so a module nobody finds fixes nothing. Two placements are wrong for that reason and are ruled out explicitly: keeping it under `components/inspector/` (nobody working on the Spec tab greps `inspector/`) and putting it under `components/workspace/` beside `specRouting.ts` (the same mistake pointed the other way — the Graph tab would then import out of the Spec tab's directory). `src/lib/` is the repo's own home for exactly this: `lib/validationHumaniser.ts:13-16` documents the leaf-no-React contract, and `components/chat/guided/stepLabels.ts:5-8` states the archetype in the plainest possible terms — *"Deliberately a LEAF module … It replaces three hand-mirrored STEP_LABELS copies that had drifted."* **The repo has already had this exact defect on the label axis and already fixed it with exactly this remedy.** That is why this plan reuses labels correctly and got topology wrong: topology is the one axis that never got the treatment. Nothing new is being invented here.

Three naming constraints follow, and they are requirements, not preferences:

1. **Keep the symbol names a searcher would actually type.** `publishedSuccessConnection`, `branchEntries`, `FAN_IN_NODE_TYPES`, `IMPLICIT_SELF_PUBLISHING_NODE_TYPES` already are those names. Do not "improve" them during the move.
2. **The module header must name the backend authority file by path**, so that `git grep _producer_resolver` run from the Python side finds the frontend mirror. The authority is `src/elspeth/web/composer/_producer_resolver.py`, function `published_success_connection` at `:98` — note the path: it is under `web/composer/`, NOT `core/`, and the same goes for `web/composer/guided/connection_consumers.py`. Both were miscited as `core/` during review; a wrong path in a header comment is worse than none, because it teaches the next reader that the mirror has no authority.
3. **The header speaks in the voice `pluginDisplayName.ts:76` uses** — "THE frontend's single title-casing implementation (elspeth-d2de348437)" — i.e. it claims the singular, so a future author who is about to write a second one reads the claim first.

Cost if wrong: one file in `lib/`, and a move if the name turns out to be wrong. Cost of getting it wrong the other way is the defect this whole task exists to fix, one axis over.

**Ruling — a branded direction-carrying type was considered and REJECTED; do not re-propose it.** A branded `InboundBranches` type, or splitting `branches` into direction-tagged shapes, would make the fan-in inversion *unwriteable* rather than merely unlikely — it is the only intervention that addresses the root cause rather than the recurrence. It is still not worth it here: it touches `types/index.ts`, `guidedDecoder.ts:2055-2062`, `test/composerFixtures.ts` and every composition fixture in the frontend suite, for a rule that one shared, tested, imported helper already protects in practice. Recorded so the next reviewer does not spend the analysis again. Cost if wrong: the rule stays enforced by a helper and its tests rather than by the type system, so someone who bypasses the helper can still write the inversion — which is why the helper is a LEAF module with an authority-naming header rather than something easy to walk past.

**Ruling — the existing GraphView coverage STAYS in `GraphView.test.tsx`; the new tests ADD unit coverage, they do not replace it.** The topology cases already in that file — notably `describe("coalesce correlated branch fan-in (elspeth-625e85c59b)")` at `:2018` and the non-terminal-coalesce test at `:2123`, which cites `_producer_resolver.published_success_connection` by name — are **integration** tests: they render `GraphView` and assert on the edges it draws. They cannot be relocated into a leaf-module unit test without losing what they check, and they are precisely the evidence that this relocation changed no behaviour. So: do not move them, do not touch them, and treat any change in their result as a failed relocation. The new `graphTopology.test.ts` adds what did not exist before — direct unit coverage of the rules themselves, which today are only reachable through a 1900-line component. Cost if wrong: two test files cover overlapping ground, which for a rule with two production incidents behind it is the right side to err on.

**Ruling — the doctrine comments move BYTE-IDENTICAL.** Do not summarise, re-word, re-wrap or "tidy" the comment blocks at `GraphView.tsx:86-135` and `:158-181` while relocating them. They are the memory of two production defects and the reason each rule is shaped the way it is; a paraphrase loses precisely the detail that would stop the next person re-deriving them. The reviewer's check is `git diff` showing pure relocation. Cost if wrong: the extraction preserves the code and discards the reason for it — the worse half.

**Ruling — the coalesce member sets move here too, and become tuples so the phrase maps can close against them.** `COALESCE_POLICIES` and `COALESCE_MERGES` already exist in the frontend, private, at `api/guidedDecoder.ts:75-76`. Task 4's `POLICY_PHRASES` / `MERGE_PHRASES` would otherwise be the **third** statement of those member sets — second in the frontend — after `core/config.py:1007` and `:1011`. That is the same archetype as the topology bug, caught before it landed rather than after, so it gets the same remedy in the same commit. Two details make this cheap and safe:

- They belong in `graphTopology.ts` rather than a new module: they are coalesce fan-in semantics, which is what this module is about, and the alternative (exporting them from a 2270-line decoder) makes a display module import the validation layer.
- They change shape from `Set<string>` to `as const` tuple + derived union type. `guidedDecoder.ts` keeps a `Set` by constructing one from the tuple, so its validation code is untouched; Task 4 gets `Record<CoalescePolicy, string>`, making an unphrased new member a **compile error** instead of silently degrading to title-cased machine text.

`types/index.ts:180-181` still types `policy`/`merge` as `string | null`, so the runtime `?? titleCaseLabel(value)` fallback in `routingPhrase` stays and is still correct — this closes the map at compile time without narrowing the wire type, which is a wider change (see the Roadmap tail). Cost if wrong: `guidedDecoder.ts` gains one import line for a set it used to declare inline.

- [ ] **Step 1: Write the failing topology tests**

`src/lib/graphTopology.test.ts` — **three** describes, covering everything this module will own. The first two pin the rules that were previously provable only through a 1900-line component; the third pins what the module gains from elsewhere — the coalesce member sets (private in `api/guidedDecoder.ts` until now) and the `discard` sentinel, which had no named home at all. Do not stop after two:

```ts
import { describe, expect, it } from "vitest";

import {
  branchEntries,
  publishedSuccessConnection,
  FAN_IN_NODE_TYPES,
  IMPLICIT_SELF_PUBLISHING_NODE_TYPES,
} from "./graphTopology";

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

describe("shared member sets and sentinels", () => {
  it("mirrors the backend coalesce Literals exactly", () => {
    // core/config.py:1007 and :1011. These were declared privately a second
    // time in api/guidedDecoder.ts:75-76; this module is now the one place
    // the frontend states them, and the `as const` union is what lets
    // Task 4's phrase maps fail the BUILD on an unphrased new member.
    expect([...COALESCE_POLICIES]).toEqual(["require_all", "quorum", "best_effort", "first"]);
    expect([...COALESCE_MERGES]).toEqual(["union", "nested", "select"]);
  });

  it("names the discard sentinel rather than spelling it three times", () => {
    // _producer_resolver.py:208 — discard is not a connection.
    expect(DISCARD_CONNECTION).toBe("discard");
  });
});
```

(Add `COALESCE_MERGES`, `COALESCE_POLICIES` and `DISCARD_CONNECTION` to the import block at the top of this file.)

Run: `npx vitest run src/lib/graphTopology.test.ts` → FAIL (module missing).

- [ ] **Step 2: Move the four helpers, the two member sets, and name the sentinel**

Create `src/lib/graphTopology.ts` with this header, then **cut** `GraphView.tsx:86-181` — the two doctrine comment blocks and the four declarations — and paste them below it unchanged, adding only the `export` keyword to each of the four:

```ts
// ============================================================================
// graphTopology — THE frontend's single model of how composition components
// join up: what a node publishes, what a fan-in node reads, and what is not
// a connection at all. Every surface that needs to answer "which component is
// on the other end of this connection?" imports from here. If you are about
// to write a second one, this is the one you were looking for.
//
// Deliberately a LEAF module — it imports ONLY types — so the Graph tab
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
//     `CoalesceSpec.branches` is a "Branch identity -> INPUT connection
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
 * producer for it). Named here because three frontend sites spelled it as a
 * bare literal and nothing tied them to that rule.
 */
export const DISCARD_CONNECTION = "discard";

/**
 * The coalesce member sets, mirrored from core/config.py:1007 and :1011 and
 * lifted out of api/guidedDecoder.ts:75-76, which held the frontend's only
 * copy privately. `as const` so consumers get a union type: a display map
 * keyed `Record<CoalescePolicy, string>` fails the BUILD when the backend
 * adds a member, instead of silently prettifying it at runtime.
 */
export const COALESCE_POLICIES = ["require_all", "quorum", "best_effort", "first"] as const;
export type CoalescePolicy = (typeof COALESCE_POLICIES)[number];

export const COALESCE_MERGES = ["union", "nested", "select"] as const;
export type CoalesceMerge = (typeof COALESCE_MERGES)[number];
```

Type the two functions structurally so both `NodeSpec` and `GraphView`'s local node shape satisfy them without a cast — `publishedSuccessConnection` already takes an inline `{ id; node_type; on_success }` shape, which is exactly right and stays; `branchEntries` already takes `string[] | Record<string, string> | null | undefined`, which is `NodeSpec["branches"]`. (The `NodeSpec` import is for the doc reference only; if `tsc` reports it unused, drop the import rather than inventing a use.)

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

and rename the two identifiers at their use sites (`git grep -n "COALESCE_POLICIES\|COALESCE_MERGES" -- src/api` finds them; there are only the declarations plus their membership checks). Nothing else in that 2270-line file changes, and `guidedDecoder.test.ts` must stay green untouched. If you would rather not rename, keep the local names and construct the sets under them — the requirement is that the MEMBERS come from `graphTopology`, not that the local identifiers change.

- [ ] **Step 3: Prove it is a pure relocation**

```bash
npx vitest run src/lib/graphTopology.test.ts src/components/inspector src/api   # PASS; GraphView.test.tsx and guidedDecoder.test.ts unchanged
npx tsc --noEmit -p tsconfig.app.json && npx tsc --noEmit -p tsconfig.test.json   # clean
git diff -- src/elspeth/web/frontend/src/components/inspector/GraphView.tsx      # deletions + one import block ONLY
git diff -- src/elspeth/web/frontend/src/api/guidedDecoder.ts                    # two declarations + one import ONLY
```

The `git diff` on `GraphView.tsx` must show removed lines plus the added import and nothing else — no re-worded comment, no reordered declaration, no changed signature. The diff on `guidedDecoder.ts` must be confined to `:75-76` and the import block. If either shows more, the relocation was not pure; revert and redo it. **Neither `GraphView.test.tsx` nor `guidedDecoder.test.ts` may appear in `git status` at all** — those two staying green and untouched is the entire proof that this task changed no behaviour, and `GraphView.test.tsx:2018` / `:2123` in particular are the cases that pin the two rules being moved.

- [ ] **Step 4: Commit**

```bash
git add src/elspeth/web/frontend/src/lib/graphTopology.ts src/elspeth/web/frontend/src/lib/graphTopology.test.ts src/elspeth/web/frontend/src/components/inspector/GraphView.tsx src/elspeth/web/frontend/src/api/guidedDecoder.ts
git commit -m "refactor(topology): lift the connection-topology rules and coalesce member sets into lib/graphTopology — one model for the graph, the Spec tab and the decoder (elspeth-93f5621f18)"
```

No ticket closes here; this is the mechanism half of `elspeth-93f5621f18` part B and is named in that ticket's closeout comment (Task 11).

---

### Task 4: Spec tab — routing values and headings in the reader register (`elspeth-93f5621f18`, part B)

**Files:**
- Create: `src/elspeth/web/frontend/src/components/workspace/specRouting.ts`
- Create: `src/elspeth/web/frontend/src/components/workspace/specRouting.test.ts`
- Modify: `src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.tsx` (`SpecRow` `:7-15`, `routingLabel`/`routingValue` `:47-69`, `SpecSection` `:71-134`, row builders `:136-220`)
- Modify: `src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.test.tsx` (`:192-294` coalesce pins, `:294-357` collector pins)

**Interfaces:**
- Consumes: `publishedSuccessConnection`, `branchEntries`, `FAN_IN_NODE_TYPES` (Task 3); `stepLabelForNodeId`, `isComponentPresent` (Task 1); `titleCaseLabel`; `sortedSourceEntries`; `expectNoIdentifiersInDefaultDom` with `allowAriaLabelSelectors` (Task 2).
- Produces (all from `specRouting.ts`):
  - `interface RoutingPhrase { text: string; raw: string }`
  - `buildConnectionIndex(state: CompositionState): ConnectionIndex` — `{ consumers: Map<string, string[]>; producers: Map<string, string[]> }` keyed by connection name; values are component ids (node ids, source keys, output names).
  - `componentPhrase(state, id): string` — `stepLabelForNodeId(state, id) ?? titleCaseLabel(id)`.
  - `routingPhrase(state, index, field, value): RoutingPhrase | null` — the whole `<dd>` for one routing field; null means "render `displayValue(value)` as before".
  - `POLICY_PHRASES`, `MERGE_PHRASES`, `OUTPUT_MODE_PHRASES` — **closed `Record`s, not `Map`s** (do not call `.get()` on them): keyed by `CoalescePolicy` / `CoalesceMerge` from `lib/graphTopology` and by `NonNullable<NodeSpec["output_mode"]>` from `types/index.ts:183`, mirroring `core/config.py:1007` `require_all|quorum|best_effort|first` and `:1011` `union|nested|select`. `SCOPE_POLICY_PHRASES` remains a `ReadonlyMap<string, string>` because it has no member set to close against. All four are read through one `ENUM_FIELDS` lookup-function table; unknown values fall to `titleCaseLabel(value)` with the raw in `title`.
- The `<h4>` heading and the `Kind` row (the `elspeth-d74ab492dd` Spec-tab items) are ALSO done here — same file, same pin, same lane — and `elspeth-d74ab492dd`'s closeout comment records that they landed in this PR. Ruling: splitting one 130-line component's copy fix across two PRs so each ticket owns "its" lines would give two reviewers half a pin each; the ticket boundary is recorded in the closeout, not enforced in git. Cost if wrong: one comment on `elspeth-d74ab492dd` pointing at this commit.

**Ruling — connection names resolve through the composition, in the direction the field means.** `on_success: raw_rows` names a CONNECTION; the reader wants the component on the other end. Which end depends on the field, and the two directions are NOT symmetric:

| Field | Direction | Why |
|---|---|---|
| `on_success`, `on_error`, `on_validation_failure`, `on_write_failure`, `fork_to`, `routes` | **downstream** — resolve through `consumers` | the node writes this connection; the reader wants who reads it |
| `input`, `branches` | **upstream** — resolve through `producers` | the node reads this connection; the reader wants who wrote it |

`branches` is the one that is easy to get backwards, and getting it backwards is not a cosmetic slip: because a fan-in node's own `input` is by convention one of its own branch connections, resolving `branches` downstream makes the node resolve to ITSELF — the flagship example renders "Branch Invest Cs1 → **Merge Invest**" instead of "→ Extract Invoice". The `branches`-are-inputs rule is `core/config.py:984-986`; the "skip `node.input` for a fan-in node" rule is `connection_consumers.py:31-40`. Both live in `graphTopology.ts` as of Task 3 and are read here, not restated.

A connection with no resolvable component (dangling mid-edit, or a branch whose producer is not yet wired) falls back to `titleCaseLabel(connection)` — the author named it, so the author-name rule applies. Cost if wrong: a dangling connection reads "Invest Cs1 Done" rather than `invest_cs1_done`; the raw stays in `title`.

**Ruling — the phrase maps close against a member set they do not own; `scope_policy` is the honest exception.** Three of the four enum maps are now keyed by a union type rather than by `string`: `POLICY_PHRASES` and `MERGE_PHRASES` off `CoalescePolicy`/`CoalesceMerge` (Task 3 lifted those members out of `api/guidedDecoder.ts:75-76`, where the frontend already held a private second copy of the `core/config.py:1007,:1011` Literals), and `OUTPUT_MODE_PHRASES` off `NonNullable<NodeSpec["output_mode"]>`, which `types/index.ts:183` already declares as a closed union. The point is the failure mode: with an open `ReadonlyMap<string, string>` plus a `?? titleCaseLabel(value)` fallback, a backend adding a policy member does not break anything — it quietly renders "Someday Maybe"-style prettified machine text at the user, and this task's own test pins that degradation as correct. Closing the maps makes it a build error, which is what an unphrased enum should be. `scope_policy` is left OPEN and stays a `Map<string, string>`, because it genuinely has no Literal and no frontend member set to bind to (`types/index.ts:191` types it `string | null`, and a collector's arrival policy is not a coalesce's) — closing a map against a set that does not exist would mean inventing the set here, which is the very thing this wave is correcting. The runtime `titleCaseLabel` fallback stays for all four, because the wire types are still `string | null` and an out-of-union value is representable; the compile-time closure is the guard, the runtime fallback is the graceful degradation behind it. Narrowing `types/index.ts:180-181,191` themselves to the backend Literals is the fuller fix and is parked in the Roadmap tail — it touches the decoder and is wider than this wave. Cost if wrong: a legitimate backend enum addition blocks the frontend build until someone writes one sentence, which is the intended trade.

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
```

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
// branches are its inputs (core/config.py CoalesceSpec.branches), and its own
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

/** Connections this node WRITES. `branches` is deliberately absent: those are
 *  what a fan-in node reads, and including them here makes it produce its own
 *  inputs. Publication goes through `publishedSuccessConnection` so a queue,
 *  coalesce or aggregation with no `on_success` is still credited with the
 *  connection it publishes under its own id. */
function nodeTargets(node: NodeSpec): string[] {
  const targets: string[] = [];
  const published = publishedSuccessConnection(node);
  if (published !== null) targets.push(published);
  if (node.on_error) targets.push(node.on_error);
  if (node.routes) targets.push(...Object.values(node.routes));
  if (node.fork_to) targets.push(...node.fork_to);
  return targets.filter((target) => target !== DISCARD_CONNECTION && target !== "fork");
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
  const consumers = new Map<string, string[]>();
  const producers = new Map<string, string[]>();
  for (const [name, source] of sortedSourceEntries(state)) {
    if (source.on_success) push(producers, source.on_success, name);
    if (source.on_validation_failure && source.on_validation_failure !== DISCARD_CONNECTION) {
      push(producers, source.on_validation_failure, name);
    }
  }
  for (const node of state.nodes) {
    for (const connection of nodeInputs(node)) push(consumers, connection, node.id);
    for (const target of nodeTargets(node)) push(producers, target, node.id);
  }
  for (const output of state.outputs) push(consumers, output.name, output.name);
  return { consumers, producers };
}

/** The shared step label, or the author-name fallback for an unlabelable id.
 *  Never "Removed": this is called with ids the index already resolved, and
 *  with connection names, where absence means dangling rather than deleted. */
export function componentPhrase(state: CompositionState, id: string): string {
  return stepLabelForNodeId(state, id) ?? titleCaseLabel(id);
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

In `PipelineSpecView.test.tsx`, update the existing pins and add the default-DOM pin (imports: add `expectNoIdentifiersInDefaultDom` from `@/test/defaultDomPins`):

1. `:192-249` ("projects a coalesce's fan-in config"): `toHaveTextContent("branch_a")` → `toHaveTextContent("Branch A → Pairing Done")`; `toHaveTextContent("hex_done")` → `toHaveTextContent("Branch B → Hex Done")`; `toHaveTextContent("require_all")` → `toHaveTextContent("wait for every branch")`; `toHaveTextContent("union")` → `toHaveTextContent("combine every branch's fields")`; add `expect(within(node).getByText("wait for every branch")).toHaveAttribute("title", "require_all");`. (Both aliases title-case rather than resolving: that fixture's only source publishes `colours_raw` and the coalesce publishes `final_out`, so nothing produces `pairing_done` or `hex_done` and the upstream lookup falls through to `titleCaseLabel` — which is the correct reading of a fixture whose upstream arms are not modelled.)
2. `:251-292` ("renders a coalesce's branch map as prose"): the `toHaveTextContent("branch_invest_cs1 → invest_cs1_done; …")` becomes `toHaveTextContent("Branch Invest Cs1 → Invest Cs1 Done; Branch Invest Cs2 → Invest Cs2 Done")` (again, no producer for either connection in that fixture) and add `expect(within(node).getByText(/^Branch Invest Cs1/)).toHaveAttribute("title", "branch_invest_cs1 → invest_cs1_done; branch_invest_cs2 → invest_cs2_done");`. The existing `expect(node).not.toHaveTextContent('{"')` assertion stays — it is the elspeth-b9ebdf9011 regression pin and is still exactly the right check.
3. `:294-355` (collector): `Scope` is a scope NAME — neither an enum nor a connection — so `routingPhrase` returns null and the caller would fall to `displayValue`, leaking `doc_pages`. Add `scope_name` to the renderer's title-case path (Step 4, `AUTHOR_NAME_FIELDS`) and pin `toHaveTextContent("Doc Pages")` with `title="doc_pages"`. `toHaveTextContent("explode_pages")` → `"Explode Pages"`; `toHaveTextContent("require_all")` → `"wait for every row in the group"`; `toHaveTextContent("passthrough")` → `"pass rows through unchanged"`; `300` unchanged.
4. `:129-190` ("shows only non-null authoritative routing fields"): unchanged assertions (labels only) — but the card's `<h4>` now reads "Rows" not "rows"; nothing there pins the heading text.
5. `:507` — no change needed. That test's `getByRole("heading", { level: 4 })` is used only as an argument to `compareDocumentPosition` (verified 2026-08-30: `PipelineSpecView.test.tsx:505-510` asserts document order, not text), so the heading's new title-cased content does not reach an assertion.
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
      allowAriaLabelSelectors: [".pipeline-spec-card"],
    });
  });
```

(`allowAriaLabelSelectors: [".pipeline-spec-card"]` covers both the article's own `aria-label` ("Node merge_invest", kept raw — 15+ existing `getByRole("article", { name })` pins depend on it) and the `OptionRows` region inside it (`"Node merge_invest settings"`, `OptionRows.tsx:194`), because `closest` walks up to the card.)

Run: `npx vitest run src/components/workspace/PipelineSpecView.test.tsx` → FAIL on the new/updated pins.

- [ ] **Step 4: Implement in `PipelineSpecView.tsx`**

1. Imports: add `import { titleCaseLabel } from "@/components/catalog/pluginDisplayName";`, `import { buildConnectionIndex, componentPhrase, routingPhrase, type ConnectionIndex } from "./specRouting";`.
2. `SpecRow` (`:7-15`): add `label: string;` after `id`. Each builder sets it: `label: componentPhrase(state, id)` (sources: `id` is the source key; nodes: `node.id`; outputs: `output.name`).
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

5. Replace `routingValue` (`:51-69`) with a renderer that consults `specRouting` first. Note this deletes the THIRD bare `"discard"` literal in the frontend (`PipelineSpecView.tsx:52`); the replacement below uses the shared `DISCARD_CONNECTION` constant, so after this task the sentinel is spelled once and is greppable across the language boundary against `_producer_resolver.py:208`. Import it: `import { DISCARD_CONNECTION } from "@/lib/graphTopology";`.

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
6. Heading and kind (`:95`, `:103-104`): `<h4 title={row.id}>{row.label}</h4>`; `<dd title={row.kind}>{titleCaseLabel(row.kind)}</dd>`. The article `aria-label` (`:93`) stays `${singular} ${row.id}` — the accessible name is the identifier by design (exempted in the pin).

Run: `npx vitest run src/components/workspace src/components/inspector` → PASS. Then `npx tsc --noEmit -p tsconfig.app.json && npx tsc --noEmit -p tsconfig.test.json` → clean.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/frontend/src/components/workspace/specRouting.ts src/elspeth/web/frontend/src/components/workspace/specRouting.test.ts src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.tsx src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.test.tsx
git commit -m "feat(spec-tab): routing values name the connected component, headings/kinds/policies in the reader register, raw ids in title (elspeth-93f5621f18)"
```

`elspeth-93f5621f18` closes at Task 11 with all three commits named (Task 1, Task 3's extraction, and this one).

---

### Task 5: Register batch (`elspeth-d74ab492dd`; absorbs the execution/consent half of `elspeth-59631ec7f7`)

One PR, one reviewer pass; commit per item so a reviewer can bisect. Every item's acceptance: the raw token is absent from visible text and present in `title`, a `data-*` attribute, or a `<code>` secondary.

**Files:**
- Create: `src/elspeth/web/frontend/src/components/chat/modelDisplayName.ts` + `.test.ts`
- Modify: `src/elspeth/web/frontend/src/components/catalog/pluginDisplayName.ts:24-39` (`ACRONYMS`)
- Modify: `src/elspeth/web/frontend/src/components/chat/ModelChip.tsx:24-30`, `ModelChip.test.tsx:18-33`
- Modify: `src/elspeth/web/frontend/src/components/settings/SecretsPanel.tsx:20-34` (`ScopeBadge`), `SecretsPanel.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/chat/AcknowledgementCard.tsx:711-716`, `AcknowledgementCard.test.tsx:510-525`
- Create: `src/elspeth/web/frontend/src/components/execution/diagnosticPhrases.ts`
- Modify: `src/elspeth/web/frontend/src/components/execution/RunsHistoryDrawer.tsx:208-263` (`RunStateFailureDetail`), `RunsHistoryDrawer.test.tsx:376-475`, `execution.css:145-149`
- Modify: `src/elspeth/web/frontend/src/components/audit/ExplainDialog.tsx:120-122`, `audit.css:346-372`, `ExplainDialog.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.tsx` (`buildRunEgressSummary` `:180-286`, render `:694-700`), `ExecuteButton.test.tsx` (16 `buildRunEgressSummary(` call sites, dialog pins `:357-361`)
- Modify: `src/elspeth/web/frontend/src/components/chat/InlineSourceCreatedTurn.tsx:190-193`, `InlineSourceCreatedTurn.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/chat/guided/ComponentReviewTurn.tsx:76-84`, `ComponentReviewTurn.test.tsx:43-44,:89-90,:100-101`

**Interfaces:**
- Produces: `modelDisplayName(modelId: string): string`; `RunEgressLine { text: string; identifiers: string }` and `buildRunEgressSummary(...): RunEgressLine[]` (same parameters as today, `:180-186`); `DIAGNOSTIC_REASON_PHRASES`, `DIAGNOSTIC_CAUSE_PHRASES` (closed `ReadonlyMap<string, string>`); `INLINE_SOURCE_PROVENANCE_LABELS: Record<InlineSourceProvenance, string>`.
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
  return titleCaseLabel(leaf.replace(/-/g, " "));
}
```

`pluginDisplayName.ts:24-39` — add `"gpt",` to `ACRONYMS` (alphabetical, after `"db"`). Ruling: the set is the frontend's single acronym list (`titleCaseLabel`'s docblock, `:76-79`) and no plugin id contains the word (verify: `grep -rn '"gpt' src/elspeth/plugins --include=*.py | head` from the repo root returns nothing at cde8a279b), so no catalog card changes. Cost if wrong: a future plugin named `gpt_something` would card as "GPT Something" — correct anyway.

`ModelChip.tsx:24-30`:

```tsx
  return (
    <span
      className="chat-model-chip"
      title={model}
      aria-label={`Composer model: ${modelDisplayName(model)}`}
    >
      <span className="chat-model-chip-label" aria-hidden="true">
        Composer:
      </span>{" "}
      {modelDisplayName(model)}
    </span>
  );
```

`ModelChip.test.tsx:18-33`: `getByLabelText("Composer model: anthropic/claude-sonnet-4.6")` → `"Composer model: Claude Sonnet 4.6"`; `getByText("anthropic/claude-sonnet-4.6")` → `getByText("Claude Sonnet 4.6")`; add `expect(screen.getByTitle("anthropic/claude-sonnet-4.6")).toBeInTheDocument();` and `expectNoIdentifiersInDefaultDom(container)` on a render with `composerModel: "openrouter/anthropic/claude-sonnet-5"`.

Run: `npx vitest run src/components/chat/modelDisplayName.test.ts src/components/chat/ModelChip.test.tsx src/components/catalog/pluginDisplayName.test.ts` → PASS. Commit: `git add` those five files; `git commit -m "feat(chat): ModelChip shows the model's display name, raw id in title (elspeth-d74ab492dd)"`.

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
    expect(screen.queryByText("user")).not.toBeInTheDocument();
  });
```

Run: `npx vitest run src/components/settings/SecretsPanel.test.tsx` → PASS. Commit.

- [ ] **Step 3 (amendment cap): implement + test**

`AcknowledgementCard.tsx:711-716`:

```tsx
          {amendIsTooLong && (
            // Characters, not bytes, in the sentence the writer reads. The
            // byte overage is an UPPER bound on the characters to remove
            // (multibyte text shortens faster), so "about" is honest; the
            // exact byte figures stay in `title` for anyone who needs them.
            <p
              className="ack-card-amend-cap-warning"
              role="status"
              title={`${amendByteLength} bytes; the maximum is ${INTERPRETATION_AMENDMENT_MAX_BYTES} bytes`}
            >
              Shorten this by about{" "}
              {amendByteLength - INTERPRETATION_AMENDMENT_MAX_BYTES} characters
              to fit the {INTERPRETATION_AMENDMENT_MAX_BYTES / 1024} KB limit.
            </p>
          )}
```

`AcknowledgementCard.test.tsx:523`: `expect(screen.getByText(/8192 bytes/)).toBeTruthy();` →

```tsx
    const warning = screen.getByRole("status");
    expect(warning).toHaveTextContent("Shorten this by about 8 characters to fit the 8 KB limit.");
    expect(warning).toHaveAttribute("title", "8200 bytes; the maximum is 8192 bytes");
```

(8200 ASCII `a`s = 8200 bytes.) Run: `npx vitest run src/components/chat/AcknowledgementCard.test.tsx` → PASS. Commit.

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

`execution.css:145-149`: the comment "Per-STATE failure provenance inside the diagnostics disclosure — …" → "Per-STATE failure provenance, rendered at one site regardless of the detail level (hoisted out of the disclosure by elspeth-34e810312c) — the same error family as .run-failure-detail (header.css), one register quieter because it nests under the per-run detail."

Run: `npx vitest run src/components/execution` → PASS. Commit.

- [ ] **Step 5 (ExplainDialog): Markdown, not `<pre>`**

`ExplainDialog.tsx:120-122`:

```tsx
          {explain && (
            <div className="explain-dialog-narrative">
              <MarkdownRenderer content={explain.narrative} />
            </div>
          )}
```

Import: `import { MarkdownRenderer } from "@/components/chat/MarkdownRenderer";`. `audit.css:346-372`: delete the "Still owed …" paragraph and the `white-space: pre-wrap;` declaration (the renderer emits paragraphs; `.markdown-body` carries the prose spacing chat already uses); keep `max-width: 74ch`, the sans family/size and `line-height`. Replace the first sentence of the comment with "Narrative prose rendered through MarkdownRenderer (elspeth-d74ab492dd; was a `<pre>`, which screen readers announce as code)."

`ExplainDialog.test.tsx:26-41`: the two `findByText`/`getByText` regexes still match (each line is its own paragraph). Add:

```tsx
  it("renders the narrative as prose paragraphs, not preformatted text (elspeth-d74ab492dd)", async () => {
    vi.mocked(api.fetchAuditReadinessExplain).mockResolvedValueOnce({
      session_id: SESSION_ID,
      composition_version: 1,
      narrative: "First paragraph.\n\nSecond paragraph.",
    });
    const { container } = render(<ExplainDialog sessionId={SESSION_ID} compositionVersion={1} onClose={() => {}} />);
    expect(await screen.findByText("First paragraph.")).toBeInTheDocument();
    expect(container.querySelector("pre")).toBeNull();
    expect(container.querySelectorAll(".explain-dialog-narrative p")).toHaveLength(2);
  });

  it("passes identifiers and punctuation through the renderer unaltered (audit fidelity)", async () => {
    // An audit narrative is generated prose that may contain node ids and
    // literal punctuation. Markdown TRANSFORMS text: `*` becomes emphasis,
    // `_snake_case_` can be eaten, a leading `#` becomes a heading. Moving
    // this surface from <pre> to a markdown renderer is only safe if the
    // narrative survives it verbatim, and a fidelity failure is a finding
    // worth having before this lands rather than after.
    vi.mocked(api.fetchAuditReadinessExplain).mockResolvedValueOnce({
      session_id: SESSION_ID,
      composition_version: 1,
      narrative: "Node submit_failed retried 3 * 2 times before the sink closed.",
    });
    const { container } = render(<ExplainDialog sessionId={SESSION_ID} compositionVersion={1} onClose={() => {}} />);
    await screen.findByText(/submit_failed/);
    expect(container.querySelector(".explain-dialog-narrative")?.textContent).toContain(
      "Node submit_failed retried 3 * 2 times before the sink closed.",
    );
  });
```

**No mermaid mock is needed.** `MarkdownRenderer.test.tsx` contains no `vi.mock` at all (verified 2026-08-30 — zero hits in that file, and no global mock in `src/test/setup.ts`), and its "renders a mermaid container" test at `:56` passes against the real `mermaid` import: `mermaid.initialize()` runs fine in jsdom. A pre-fix draft of this step told the implementer to copy a mocking pattern from that file that does not exist there. If the import does trip during implementation, that is new information — diagnose it then, do not mock `MarkdownRenderer` itself (the paragraph count is the assertion).

Run: `npx vitest run src/components/audit/ExplainDialog.test.tsx src/styles/classNames.test.ts` → PASS. Commit.

- [ ] **Step 6 (egress lines): reader text + identifier title**

`ExecuteButton.tsx` — above `buildRunEgressSummary` (`:180`):

```ts
export interface RunEgressLine {
  /** Reader register: step labels and plugin display names. */
  text: string;
  /** Identifier register — the exact sentence this dialog showed before
   *  Wave 3, unchanged so every component and plugin is still named by id
   *  (R2-F7). Rendered in `title`. */
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

- ordinary sources (`:203`): `` `${sourceComponentId(sourceName)} (${source.plugin})` `` → `` `${register === "identifier" ? sourceComponentId(sourceName) : component(sourceName)} (${plugin(source.plugin)})` `` (`stepLabelForNodeId` resolves sources by their KEY, `interpretationStepLabel.ts:105`, so pass `sourceName`, not the `source:` composite);
- LLM sources (`:211`): same substitution for the name; `llmSourceBindingLabel(source)` is unchanged (profile aliases are already reader-safe by construction, `:100-108`);
- LLM nodes (`:225-228`): `` `${component(node.id)} (model ${model(node.options.model)})` `` / `component(node.id)`;
- network / unverifiable nodes — **four sites, at `:242`, `:253`, `:256` and `:265`** (re-verified 2026-08-30; a pre-fix draft cited `:240,:255,:258,:268`, which land on a filter predicate, a filter predicate, a bare comment line and the unrelated `if (networkNodes.length > 0) {` check): `` `${component(node.id)} (${plugin(node.plugin as string)})` ``. All four are the same literal, so grep for `` `${node.id} (${node.plugin})` `` rather than trusting the line list, and expect exactly four hits;
- outputs (`:277`): `` `${component(output.name)} (${plugin(output.plugin)})` ``.

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

(Both passes walk the same composition in the same order, so the arrays align by construction — `register` only substitutes label TEXT, it never gates whether a sentence is emitted. That is a property of the code as written and could silently stop holding, so make it checkable rather than asserted in prose: add `if (reader.length !== identifiers.length) throw new Error("egress registers disagreed on line count");` before the `map`. A thrown error on a run-confirm dialog is a loud, fixable failure; a silently misaligned `title` on an audit-required egress line is not.) Imports: `pluginDisplayName`, `titleCaseLabel` from `@/components/catalog/pluginDisplayName`; `stepLabelForNodeId` from `@/components/chat/interpretationStepLabel`; `modelDisplayName` from `@/components/chat/modelDisplayName`. Render (`:696-698`): `<li key={line.identifiers} title={line.identifiers}>{line.text}</li>`. The docblock (`:146-179`) gains one line: "Returns reader-register lines with the identifier-register sentence beside each for `title`."

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

Dialog pins `:357-361`: `"source (csv)"` → `"Source (CSV)"`; the LLM line → `"Classify (model Claude Sonnet 4.6)"`; `"results (csv)"` → `"Results (CSV)"`; add `expect(within(dialog).getByText("Reads source data: Source (CSV).")).toHaveAttribute("title", "Reads source data: source (csv).")`. Add a default-DOM pin on the open dialog: `expectNoIdentifiersInDefaultDom(screen.getByRole("alertdialog", { name: "Run pipeline" }))`. (`makeComposition` here is the file's own 13-line helper at `ExecuteButton.test.tsx:99-111`, NOT an import from `@/test/composerFixtures` — verified 2026-08-30; a pre-fix draft cited `:31-113`, a range that mostly covers unrelated fixtures such as `READY_READINESS`.)

Run: `npx vitest run src/components/sidebar` → PASS. Commit: `"feat(run-confirm): egress lines in the reader register with the identifier sentence in title — every sentence kept (elspeth-d74ab492dd, elspeth-59631ec7f7)"`.

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

Run: `npx vitest run src/components/chat/InlineSourceCreatedTurn.test.tsx` → PASS. Commit.

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

Run: `npx vitest run src/components/chat/guided/ComponentReviewTurn.test.tsx` → PASS. Commit.

- [ ] **Step 9: Whole-directory run + typecheck**

`npx vitest run src/components/chat src/components/settings src/components/execution src/components/audit src/components/sidebar src/components/catalog src/styles` → PASS; `npx tsc --noEmit -p tsconfig.app.json && npx tsc --noEmit -p tsconfig.test.json` → clean; `npm run lint` → clean.

Ticket disposition at Task 11: close `elspeth-d74ab492dd` (comment lists the eight items + the Spec-tab `<h4>`/kind/policy items landed in Task 4's commit) and `elspeth-59631ec7f7` (comment states the one rule and the two commits that apply it).

---

### Task 6: Freeform brief — reply in the reader's terms (`elspeth-4bf65fe149`)

**Files:**
- Modify: `src/elspeth/web/composer/skills/pipeline_composer.md` (insert a section before `## Requested Workflow Integrity`, `:134`; add a checklist line in `## Termination States`, `:851-860`)
- Modify: `tests/unit/web/composer/test_prompts.py` (brief-content pin)

**Interfaces:**
- Consumes: `SYSTEM_PROMPT` (`prompts.py:66`, rendered from `_PIPELINE_SKILL` = `pipeline_composer.md` via `load_skill_with_hash`; `test_prompts.py:35` already imports it).
- The skill hash (`composer_skill_hash` audit column, `skills/__init__.py:38-60`) is computed at load time, not pinned in the tree (verified: no 64-hex literal in `test_prompt_cache_layout.py` / `test_capability_skill_identity.py`; `test_compose_loop_interpretation_review_dispatch.py:2869-2899` derives the pristine hash dynamically). Changing the text changes the hash for every subsequent audit row — that is the honest record of a brief change, not churn.

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
        ):
            assert phrase in section, phrase

    def test_termination_checklist_includes_the_register_line(self) -> None:
        checklist = SYSTEM_PROMPT.split("## Termination States", 1)[1]
        assert "no tool-argument keys, validation fields, or enum values in prose" in checklist
```

Run: `source .venv/bin/activate && pytest tests/unit/web/composer/test_prompts.py -k ReplyRegister -q` → 3 FAIL.

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
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth > /tmp/w3-lints-task5.txt; diff /tmp/w3-lints-before.txt /tmp/w3-lints-task5.txt
```

Expected: empty (a Markdown file and a `tests/` edit cannot move a `src/elspeth` corpus; the run is the proof).

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
- The scenario `populated-long-transcript` seeds a composition with one source (`source`, csv) and one output (`results`, csv) and NO nodes (`workspace-fixtures.ts:113-190`) — two entries in the a11y list, which is enough to Tab between items. It does NOT use `tall-confirmation-dialog`, so `elspeth-71bbf7eb12` (the tall-dialog `/validate` timeout) cannot affect this spec.
- The list: `<ol className="graph-a11y-list" aria-label="Pipeline components in source-to-sink order (N)">` (`GraphView.tsx:1888-1890`), one `<button>` per component; activation calls `selectNode` and sets `focusPanelOnOpenRef` so the config panel (`<aside role="complementary" aria-label="<id> configuration" tabIndex={-1}>`, `:685-693`) receives focus (`:810-817`). CSS: `.graph-a11y-list` is a 1px clip, `.graph-a11y-list:focus-within` reveals it (clip `inspector.css:337-348`, reveal `:351-365`).

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

- [ ] **Step 2: Run it (one Playwright process per worktree)**

From `src/elspeth/web/frontend`: `npx playwright test tests/e2e/composer-workspace-graph-keyboard.spec.ts` → PASS. If `items.first()` is not the element focus lands on first (Tab order puts the source button first because sources precede outputs in `accessibleNodes`, `GraphView.tsx:1807-1810`), read the failure and fix the ASSERTION order, not the component. Then `npx tsc --noEmit -p tsconfig.e2e.json` → clean (one pre-existing TS7016 for `scripts/staging-tutorial-driver.mjs` is `elspeth-062c1d0b7f`, not this task) and `npm run lint` → clean.

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

Expected: the spec now FAILS on `expect(revealed.width).toBeGreaterThan(1)`. Remove the injected line, re-run → PASS. Record both runs in the commit message.

**Ruling — inject the override, never edit the stylesheet in place; and state the narrower claim honestly.** This is a shared checkout with another lane's uncommitted work in it, so `git checkout -- inspector.css` is forbidden outright (Global Constraints), and a `cp` backup/restore round-trip is the same hazard in a narrower window: for the duration of the run the tracked file holds content nobody staged, and a sibling's `git add -A` or a failed restore keeps it. What `addStyleTag` proves is slightly weaker than editing the source: it shows the assertion is load-bearing on the list's REVEALED GEOMETRY — kill the reveal and the spec goes red — rather than that this particular CSS block is the only thing producing it. That is the property the spec is for, and it is worth the honesty. The stronger control (edit `inspector.css` inside a throwaway `git worktree`, run there, delete the worktree) is available if a reviewer wants the source-level version; it costs a worktree and a second Playwright run, and **must not overlap the first** — Playwright auth state is worktree-global and two concurrent runs corrupt it. Cost if wrong: the control passes while some other rule also collapses the list, and the spec is a slightly weaker guard than advertised — recorded here rather than discovered later.

- [ ] **Step 4: Commit**

```bash
git add src/elspeth/web/frontend/tests/e2e/composer-workspace-graph-keyboard.spec.ts
git commit -m "test(e2e): keyboard path through the graph a11y component list — 1px clip, :focus-within reveal, Enter focuses the config panel; negative control run (elspeth-d1feee1e67)"
```

---

### Task 8: Minor `show_advanced` gates (`elspeth-f1394307e3`)

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/recovery/RecoveryPanel.tsx` (`:1-6` imports, `:46` state, `:158-178` transcript block), `RecoveryPanel.test.tsx` (`:92-114`)
- Modify: `src/elspeth/web/frontend/src/components/blobs/BlobRow.tsx` (`:141-144` summary derivation, `:263-275` block), `BlobRow.test.tsx` (`:98,:125,:182,:208` structure pins)
- Modify: `src/elspeth/web/frontend/src/components/audit/AuditReadinessPanel.tsx:521-542` (Refresh), `AuditReadinessPanel.test.tsx` (`:396`, `:494`)

**Interfaces:**
- Consumes: `useShowAdvanced()`, `resetStore(usePreferencesStore)`, `expectNoIdentifiersInDefaultDom`.
- "Show archived" (HeaderSessionSwitcher) is NOT in scope — it is the only archive-restore path (ticket text).

**Ruling — omit, don't hint:** with the flag off each surface loses only the technical control; its sibling content is the plain summary (RecoveryDiff + Discard/Apply; the blob preview; the audit panel, which already refetches on every composition version, `AuditReadinessPanel.tsx:329-353`). No "turn on Detail level to see…" copy — the Wave 2 `CompletionBar` precedent (`CompletionBar.tsx:103`, Import YAML simply absent). Cost if wrong: a user who wants the transcript must know the preference exists — it is documented on the preferences panel.

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

`BlobRow.test.tsx` — same three imports + top-level `beforeEach(() => resetStore(usePreferencesStore));`; the four tests that `findByTestId("blob-row-structure")` (`:98,:125,:182,:208`) get `usePreferencesStore.setState({ showAdvanced: true });` as their first line. Add (copy the CSV mock setup from the `:85-103` test):

```tsx
  it("keeps the structural self-disclosure out of the default DOM; the preview itself stays (elspeth-f1394307e3)", async () => {
    const user = userEvent.setup();
    const { container } = render(<BlobRow blob={makeBlob({ mime_type: "text/csv" })} sessionId="session-1" onDownload={vi.fn()} onDelete={vi.fn()} onUseAsInput={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: /preview data\.csv/i }));
    await screen.findByText(/name,age/); // the preview body rendered
    expect(screen.queryByTestId("blob-row-structure")).not.toBeInTheDocument();
    // The wave's acceptance gate. If the preview fixture's own content trips
    // the snake_case scan (a column header like `row_id`), exempt the preview
    // BODY by selector — `allowSelectors: [".blob-row-preview"]` or whatever
    // the body element's class is — never by weakening the helper.
    expectNoIdentifiersInDefaultDom(container);
  });
```

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

(The refetch `useEffect` depends on `compositionState?.version`, `AuditReadinessPanel.tsx:329-353` — bump the VERSION, not some other field, or the effect will not re-run. Use the fetch mock this file already installs; read its name and its `beforeEach` before writing, and import `act`/`waitFor` from `@testing-library/react` if they are not already imported.)

Run: `npx vitest run src/components/recovery src/components/blobs src/components/audit/AuditReadinessPanel.test.tsx` → the four new tests FAIL (the cadence test may pass immediately, since it pins behaviour this task must PRESERVE rather than add — that is expected and correct; it fails only if the implementation breaks the cadence); the flag-on tests PASS.

- [ ] **Step 2: Implement**

`RecoveryPanel.tsx`: `import { useShowAdvanced } from "@/stores/preferencesStore";`; `const showAdvanced = useShowAdvanced();` after `:46`; wrap `:158-178` — the `recovery-panel-transcript-controls` div AND `<RecoveryTranscript …/>` — in `{showAdvanced && (<>…</>)}` with the comment "Raw tool transcript is engineer-register (tool names, call ids, raw responses); RecoveryDiff + the two actions above/below are the audit-required summary and stay (elspeth-f1394307e3)."

`BlobRow.tsx`: import `useShowAdvanced`; `const showAdvanced = useShowAdvanced();` near `:141`; the `useMemo` at `:141-144` returns `null` early when `!showAdvanced` (so the introspection is not even computed): `if (!showAdvanced || previewContent === null) return null;` and add `showAdvanced` to its dependency array. Nothing else changes — the status dot, creator badge and four actions are untouched (`elspeth-50fd9b04e0` neutral-trigger rule).

`AuditReadinessPanel.tsx`: import `useShowAdvanced` (the file's hook block, `:14`); `const showAdvanced = useShowAdvanced();` with the other hooks (before any early return); wrap the Refresh `<Button …>` (`:522-542`) in `{showAdvanced && (…)}` with the comment "The panel refetches on every composition version (useEffect above); a manual Refresh is a debugging affordance (elspeth-f1394307e3). Explain and Collapse stay."

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
- Modify: `src/elspeth/web/frontend/src/components/catalog/AuditCharacteristicIcon.test.tsx:38-47` (two tests: the raw-flag fallback at `:38-41` and the class assertion at `:43-47` — BOTH go, the second because it asserts the very class this task deletes)
- Modify: `src/elspeth/web/frontend/src/components/catalog/catalogClassNames.test.ts:114-118` (`audit-icon-unknown` entry)
- Modify: `src/elspeth/web/frontend/src/styles/classNames.test.ts:306-309` (**the sibling `audit-icon-unknown` entry in the WHOLE-TREE gate** — see the ruling)
- Modify: `src/elspeth/web/frontend/src/components/catalog/PluginCard.test.tsx` (the acceptance pin — see Step 1)

**Interfaces:** `AuditCharacteristicIcon` returns `null` for a flag with no metadata. The closed-set guard is `tests/unit/web/catalog/test_audit_characteristic_vocabulary_parity.py:38` (`test_audit_characteristic_python_matches_ts_metadata`) — it already fails CI on vocabulary drift, so the chip only ever fired when that gate was already red. `composer_tier_default` was deleted in Wave 1 (`elspeth-9cca900d41`) — do not touch it.

**Ruling — `audit-icon-unknown` is recorded in TWO allowlists and both entries die in this commit.** The name sits in `catalogClassNames.test.ts:114-118` AND in the whole-tree gate `src/styles/classNames.test.ts:306-309`, whose own reason text ends "Also recorded in catalogClassNames.test.ts" — i.e. the duplication is deliberate, a sibling record, not a copy to ignore. `classNames.test.ts`'s "keeps the rule-less allowlist honest" test (`:442-451`) asserts every allowlisted name is still applied by some product TSX (`componentSources` at `:374` walks the whole `src` tree). Deleting the class's only application site (`AuditCharacteristicIcon.tsx:21`, confirmed its only one) while leaving that entry standing fails that test with "allowlisted but no component applies it" — so a pre-fix draft of this task, which touched only the catalog allowlist, claimed a `PASS` on `src/styles/classNames.test.ts` that it could not have got. Cost if wrong: the whole-tree gate goes red for every sibling lane on the branch, which is the failure mode the Global Constraints call out by name. (`audit-icon-label` is ALSO in both allowlists and STAYS in both — the metadata branch that applies it is untouched.)

- [ ] **Step 1: Replace the two tests and pin the acceptance one level up**

`AuditCharacteristicIcon.test.tsx:38-47` — delete both tests (`"renders unknown flags as a fallback chip with the raw flag string"` and `"applies an 'audit-icon-unknown' class for unknown flags"`) and put this in their place:

```tsx
  it("renders nothing for a flag outside the closed vocabulary — drift is the parity test's job, not a chip's (elspeth-0bfd019f68)", () => {
    const { container } = render(<AuditCharacteristicIcon flag="future_flag_2027" />);
    expect(container).toBeEmptyDOMElement();
  });
```

That is the mechanism pin. The wave's default-DOM acceptance pin goes on `PluginCard` instead — `AuditCharacteristicIcon`'s only consumer (`PluginCard.tsx:199`) and the surface where a raw backend flag would actually have reached a user. On the icon's own container the pin would scan an empty element and could not fail; on the card it is a real check. Add to `PluginCard.test.tsx` (reuse the file's existing plugin fixture and render helper; give the fixture an `audit_characteristics` array containing one in-vocabulary flag and one out-of-vocabulary flag):

```tsx
  it("shows no raw backend flag for a characteristic outside the closed vocabulary (elspeth-0bfd019f68)", () => {
    const { container } = render(<PluginCard plugin={makePlugin({ audit_characteristics: ["records_llm_calls", "future_flag_2027"] })} />);
    expect(screen.queryByText("future_flag_2027")).not.toBeInTheDocument();
    expectNoIdentifiersInDefaultDom(container);
  });
```

(Import `expectNoIdentifiersInDefaultDom` from `@/test/defaultDomPins`; substitute the file's real fixture factory and the real `PluginCard` props — read the file first. `"records_llm_calls"` stands for any flag that IS in `auditCharacteristics`; grep that module and use a real one, so the test proves the known chip still renders while the unknown one does not.)

Run: `npx vitest run src/components/catalog/AuditCharacteristicIcon.test.tsx src/components/catalog/PluginCard.test.tsx` → FAIL (chip still renders).

- [ ] **Step 2: Delete the chip and its allowlist entry**

`AuditCharacteristicIcon.tsx`:

```tsx
// ============================================================================
// AuditCharacteristicIcon
//
// Single-flag renderer used by the plugin card and the filter chip strip.
// Looks up the flag in the centralised metadata table. A flag with no
// metadata renders NOTHING: the Python↔TS vocabulary parity test
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

Verify the class is gone tree-wide and that no stylesheet rule ever existed for it: `git grep -n "audit-icon-unknown" -- src` returns the four sites before the change (component, its test, both allowlists) and **nothing** after.

Run: `npx vitest run src/components/catalog src/styles/classNames.test.ts` → PASS, including `classNames.test.ts`'s "keeps the rule-less allowlist honest" test (`:442-451`), which is the test that would have caught a one-sided deletion. Backend guard unchanged: `pytest tests/unit/web/catalog/test_audit_characteristic_vocabulary_parity.py -q` → PASS.

- [ ] **Step 3: Commit**

```bash
git add src/elspeth/web/frontend/src/components/catalog/AuditCharacteristicIcon.tsx src/elspeth/web/frontend/src/components/catalog/AuditCharacteristicIcon.test.tsx src/elspeth/web/frontend/src/components/catalog/PluginCard.test.tsx src/elspeth/web/frontend/src/components/catalog/catalogClassNames.test.ts src/elspeth/web/frontend/src/styles/classNames.test.ts
git commit -m "refactor(catalog): delete the unknown-audit-characteristic fallback chip and both rule-less allowlist entries; parity test is the drift guard (elspeth-0bfd019f68)"
```

Ticket closes at Task 11 (its other item was Wave 1's).

---

### Task 10: Preferences payload decoder (`elspeth-7d07df6438`, bug, absorbed)

**Files:**
- Create: `src/elspeth/web/frontend/src/api/preferencesDecoder.ts` + `preferencesDecoder.test.ts`
- Modify: `src/elspeth/web/frontend/src/api/client.ts:754-771` (`fetchUserComposerPreferences`, `updateUserComposerPreferences`)

**Interfaces:**
- Produces: `decodeUserComposerPreferences(value: unknown): UserComposerPreferencesPayload` — exact record (`types/api.ts:96-114`: ten keys), throws `Error("Invalid composer preferences at <path>: <detail>")` on a missing/extra key or wrong type. Both client functions call it on the parsed body; `parseResponse<unknown>` stays the transport (its 401 interceptor at `:212-216` is untouched).
- Consumes: the `guidedDecoder.ts` idiom (`:102-128`: `invalid`/`record`/`exactRecord`; `:171-178`: `nullableString`/`booleanValue`) — **copied, not imported. This is a known, measured duplication, parked with a ticket, not a clean reuse; see the ruling below.**

**Ruling — the decoder primitives are COPIED in this wave, and that is recorded as a second instance of the archetype rather than presented as clean.** A structural sweep found this is the same shape as the Task 3 topology defect: `api/guidedDecoder.ts` holds eight private primitives (`invalid` `:102`, `record` `:106`, `exactRecord` `:113`, `stringValue` `:130`, `nullableString` `:171`, `booleanValue` `:175`, `integerValue` `:180`, `stringArray` `:190`) and exports only its four `decode*Response` functions (`:2216`, `:2220`, `:2264`, `:2268`), so a second decoder can only copy them. The right fix is a `src/api/decodePrimitives.ts` leaf with the error prefix injected. It is NOT done here, and the reason is measured rather than asserted:

- The eight primitives are called **487 times** inside `guidedDecoder.ts` (`invalid` alone 173, `exactRecord` 92, `stringValue` 134 — counted 2026-08-30).
- `invalid` hardcodes its prefix in its own body: `` throw new Error(`Invalid guided response at ${path}: ${detail}`) ``. Parameterising it means either threading a prefix through all 173 call sites or converting the module to a factory bundle, which changes how all 487 sites resolve their helpers.
- The cheap-looking shortcut is worse than the copy: simply exporting the primitives unchanged would give the preferences decoder error messages reading "Invalid **guided response** at show_advanced", i.e. a decoder that lies about which payload failed. A copy with an honest prefix beats a shared helper with a false one.

That is a refactor of a 2270-line validation module inside a wave whose scope is copy register, so it is parked (Roadmap tail) rather than absorbed. What changes here is only the honesty of the record: this is the second instance of the archetype in one plan, and it is named as such. Cost if wrong: two decoders drift in their error-message shape or their exact-record strictness until the ticket lands — bounded, because both are pinned by their own tests.

**Ruling — fail closed, no `undefined` into the store:** the bug is `show_advanced` omitted → `undefined` → `<details open={undefined}>` closed by accident, a silent wrong default on every Wave 1/2 surface. A thrown decode error surfaces in `preferencesStore.bootstrap`'s existing error path (the store's `error` field / banner cluster, `preferencesStore.test.ts:682`). Cost if wrong: a backend that legitimately adds a field breaks preference loading until the decoder learns it — the exact-record discipline every other decoder in this frontend already imposes.

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
});
```

Run: `npx vitest run src/api/preferencesDecoder.test.ts` → FAIL (module missing).

- [ ] **Step 2: Implement**

```ts
// ============================================================================
// preferencesDecoder — structural decoder for the account-level composer
// preferences payload (elspeth-7d07df6438). `parseResponse` is an unchecked
// `as T` cast; before this decoder an omitted `show_advanced` loaded
// `undefined` into preferencesStore and every `<details open={undefined}>`
// closed by accident. Same exact-record discipline as api/guidedDecoder.ts.
// ============================================================================

import type {
  ComposerMode,
  PersistedTutorialStage,
  UserComposerPreferencesPayload,
} from "@/types/api";

const MODES: ReadonlySet<string> = new Set<ComposerMode>(["guided", "freeform"]);
const STAGES: ReadonlySet<string> = new Set<PersistedTutorialStage>(["guided", "run", "audit", "graduation"]);
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
  if (typeof mode !== "string" || !MODES.has(mode)) invalid(`${path}.default_mode`, "expected guided|freeform");
  const stage = r.tutorial_stage;
  if (stage !== null && (typeof stage !== "string" || !STAGES.has(stage))) {
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
git add src/elspeth/web/frontend/src/api/preferencesDecoder.ts src/elspeth/web/frontend/src/api/preferencesDecoder.test.ts src/elspeth/web/frontend/src/api/client.ts
git commit -m "fix(preferences): decode the account-preferences payload structurally on GET and PATCH — an omitted show_advanced fails closed (elspeth-7d07df6438)"
filigree update elspeth-7d07df6438 --actor <lane> --status verifying \
  -f fix_verification="preferencesDecoder.test.ts covers four independent failure axes (omitted key, wrong type, unknown enum value, extra key) plus the happy path; client.ts wired on both GET and PATCH; npx vitest run src/api src/stores/preferencesStore.test.ts src/components/settings green; npx tsc --noEmit -p tsconfig.app.json clean. Live: Task 11 Check 5 (Detail level radio round-trips)."
```

**`fix_verification` is not optional and must be set in THIS command.** `verifying → closed` is the bug template's one HARD transition and requires that field (`filigree type-info bug`), and `filigree close` has no `--field`/`-f` option to supply it later — so a Task 11 close loop that reaches this ticket without it aborts with `Cannot transition 'verifying' -> 'closed' for type 'bug': missing required fields: fix_verification`. `filigree update` applies the status change and the fields atomically, which is why they go together here. (`severity` and `root_cause` were set at claim time, Task 0 Step 2.)

Close at Task 11 after the full suite.

---

### Task 11: Whole-tree verification, live check, and closeout

**Files:** none new. Runs on the merged integration branch (all task branches landed), not on any single lane's branch.

- [ ] **Step 1a: Frontend full run**

From `src/elspeth/web/frontend`: `npx vitest run`, then `npx tsc --noEmit -p tsconfig.app.json && npx tsc --noEmit -p tsconfig.test.json && npx tsc --noEmit -p tsconfig.e2e.json`, then `npm run lint` and `npm run lint:css`. Expected: all green, including `src/styles/classNames.test.ts`, `catalogClassNames.test.ts` (the allowlist-honesty test after Task 8), `executionClassNames.test.ts`, and every default-DOM pin added in Tasks 1, 2, 4, 5, 8 and 9.

- [ ] **Step 1b: Frontend e2e (sequential, one worktree)**

`npx playwright test tests/e2e/composer-workspace-graph-keyboard.spec.ts tests/e2e/composer-workspace-accessibility.spec.ts tests/e2e/composer-preferences.spec.ts` — the new spec, axe over the changed surfaces, and the preference round-trip (Task 10 changed the decoder both fetch paths use). Do not include `composer-workspace-geometry.spec.ts`'s tall-dialog scenario as a Wave 3 gate (`elspeth-71bbf7eb12`, P1, out of scope); if the geometry spec is run and only `assertTallDialogLivePreflight` times out, record it under that ticket, not this wave.

**Why three specs is the whole e2e blast radius, stated so a reviewer need not re-derive it:** the wave changes visible strings, so the exposure is e2e specs that PIN those strings. Checked 2026-08-30 — `composer-workspace-geometry.spec.ts:122` asserts only that `acknowledgement-card` is VISIBLE, never its title; `composer-workspace-accessibility.spec.ts:151` only opens the Spec tab without asserting a heading's text; and no e2e spec anywhere pins a run-confirm egress sentence or a Spec-tab `<h4>`. So no existing spec can go red on a copy change, and the three above are run because they exercise the surfaces this wave ADDS or rewires, not because the rest are at risk.

- [ ] **Step 2: Lint corpus diff (backend-touching: Task 6 only)**

```bash
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth > /tmp/w3-lints-after.txt
diff /tmp/w3-lints-before.txt /tmp/w3-lints-after.txt; grep -c . /tmp/w3-lints-after.txt  # stdout only; grep -c exits 1 on zero — do not wrap in set -e
```

Expected: no added findings, identical counts (COUNT the corpus, never `tail` it).

- [ ] **Step 3: Backend full suite as a background worktree job**

```bash
git worktree add .claude/worktrees/wave3-verify HEAD
ln -s "$(pwd)/.venv" .claude/worktrees/wave3-verify/.venv 2>/dev/null || true
cd .claude/worktrees/wave3-verify
PYTHONPATH=$(pwd)/src:$(pwd)/elspeth-lints/src .venv/bin/python -c "import elspeth, elspeth_lints; print(elspeth.__file__, elspeth_lints.__file__)"  # both must point into the worktree
PYTHONPATH=$(pwd)/src:$(pwd)/elspeth-lints/src .venv/bin/python -m pytest tests/ -n 12 -q 2>&1 | tail -30
```

Run in the background; read the summary line, not `tail` alone (confirm a non-zero collected count). Must-be-green list: `tests/unit/web/composer/test_prompts.py`, `test_tool_declarations.py`, `test_prompt_cache_layout.py`, `test_capability_skill_identity.py`, `test_skills_loader.py`, `test_compose_loop_interpretation_review_dispatch.py`, `tests/unit/web/catalog/test_audit_characteristic_vocabulary_parity.py`, `tests/unit/web/test_sessions_composer_attribute_contracts.py`, `tests/unit/elspeth_lints/test_masquerade_gate.py`, and the knob-schema goldens untouched (`git status --short tests/golden` empty).

- [ ] **Step 4: Live check on session 39578c6f**

Executor: the hub, on the merged integration branch. `npm run build` in `src/elspeth/web/frontend`; because Task 6 changed a Python-packaged file (`skills/pipeline_composer.md` is read at import via `load_skill_with_hash`), restart with the exact sudoers form `sudo -n /usr/bin/systemctl restart elspeth-web.service`, then poll `/api/system/status` until `frontend_build` shows the new build id (`is-active` lies after a restart). Site https://elspeth.foundryside.dev, throwaway eval login `<staging-username>` / `<staging-password>`. An API PATCH of preferences does not reach a mounted store — flip the in-app "Detail level" radio for the flag-on pass.

1. **Spec tab at the default preference** (the wave's acceptance): walk every card on 39578c6f v19; count snake_case text nodes outside `<code>`/`<pre>`/`<details>` and compare with the Wave 1 epic-comment baseline (Task 0 Step 3). Expected: the 13 node-id headings, every routing `<dd>`, and the policy/kind values are gone from visible text; hovering a heading or routing value shows the raw id in the tooltip. Remaining hits must be one of: OptionRows `field_mapping`/user-keyed map keys (verbatim by design, Wave 1), or prompt-template excerpt field names (`case_study1` — authored content, ruling below). Anything else REOPENS `elspeth-93f5621f18`.
2. **Acknowledgement card for a deleted node:** delete a node that has a pending card (or use a session with one); the card title reads "Removed step · …"; the wire-blocker jump link matches; the DOM node carries `data-affected-node-id`.
3. **Freeform reply register:** in a fresh freeform session ask for a two-branch pipeline with a coalesce; the final reply names steps by label, says "waits for every branch" (or equivalent) rather than `require_all`, says "validation passed" rather than `is_valid: true`, and contains no ASCII topology tree. This is a planner-behaviour observation, not a pass/fail gate — record the reply verbatim in the epic comment; if it still leaks, the brief needs another turn (a new ticket), not a server-side filter.
4. **Register batch:** header chip shows "Composer: Claude Sonnet 5" (or the deployment's model) with the raw id on hover; Secrets panel badges read Yours/Deployment; run-confirm dialog lines read "Source (CSV)", "Fetch Page (Web Scrape)" with the identifier sentence on hover; Explain dialog renders paragraphs; run history's curated failure row (if the session has one) shows the prose reason.
5. **Minor gates:** default preference → no Refresh on the audit panel, no structural summary under a blob preview, no transcript in a recovery panel (trigger one only if a recovery error is reproducible; otherwise rely on the unit pin); flag on → all three present.
6. **Catalog:** every card's audit chips render; no grey "unknown" chip anywhere (none expected — the parity test is green).
7. **Keyboard path:** Tab from the Graph tab into the component list; the panel reveals; Enter opens "… configuration" with focus in it. (The e2e proves it on the Playwright backend; this confirms the deployed build.)
8. **Manual AT check (Wave 2 deferral, closed-`<details>` aria-describedby):** with a screen reader (NVDA on Windows or VoiceOver on macOS — whichever the operator has), focus a SchemaFormTurn field whose description sits inside a CLOSED "Advanced settings" `<details>` (`SchemaFormTurn.tsx:560,:600` reference description ids; the advanced fields live under the `<details>` Wave 1 added). Record whether the description is announced. Outcome goes in the epic comment; if it is NOT announced, open a ticket (the fix is to keep description text outside the `<details>` or use `aria-description`) — no code in this wave.
9. Run the tutorial once end to end — ADR-031 canary, unchanged.

Report: one `filigree add-comment elspeth-cd8abcba3f` listing each check pass/fail with the frontend build id and the merged sha; a failed check REOPENS that task's ticket rather than being noted in prose.

- [ ] **Step 5: Ticket mechanics**

**The eight task-type tickets close with nothing extra.** `elspeth-93f5621f18`, `elspeth-d74ab492dd`, `elspeth-4bf65fe149`, `elspeth-d1feee1e67`, `elspeth-f1394307e3`, `elspeth-0bfd019f68`, `elspeth-13b69b5846`, `elspeth-59631ec7f7` are all type `task`, whose template has `open → closed` and `in_progress → closed` as direct SOFT transitions with **no required fields** (`filigree type-info task`) — so no `-f` flag is needed on any of them, from either state:

```bash
filigree add-comment <id> "<what landed, commit shas, what was verified (tests + live check number)>" --actor <assignee>
filigree close <id> --actor <assignee>
```

**The bug is different — check its field BEFORE running the loop.** `elspeth-7d07df6438` should already be `verifying` with `fix_verification` set (Task 10 Step 3). Confirm it, because the close HARD-fails without it and there is no way to supply it on `filigree close`:

```bash
filigree show elspeth-7d07df6438           # expect: Status verifying, fix_verification present
filigree add-comment elspeth-7d07df6438 "<what landed, commit sha, tests + live check 5>" --actor <assignee>
filigree close elspeth-7d07df6438 --actor <assignee>
```

If `fix_verification` is missing (the lane closed out early, or the update was run without `-f`), set it first with `filigree update elspeth-7d07df6438 --actor <assignee> -f fix_verification="<how it was verified>"` and then close. Do NOT reach for `--force`: that uses the template's escape transition and records a close that skipped the workflow, which is worth avoiding for a one-flag fix.

Specifics: `elspeth-93f5621f18`'s comment records the "Removed" wording ruling and the three-state fallback, and names all three commits (Task 1, Task 3's topology extraction, and Task 4). `elspeth-d74ab492dd`'s comment records that its Spec-tab `<h4>`/kind/policy items landed in Task 4's commit, lists the eight register items with their commits, and the `"gpt"` acronym addition. `elspeth-59631ec7f7`'s comment states the one rule verbatim (Global Constraints) and the two commits applying it (Task 2 catalog row, Task 5 egress lines). `elspeth-4bf65fe149`'s comment records the test-module ruling (`test_prompts.py`, not `test_pipeline_planner.py`), the no-fixture ruling, and Check 3's verbatim reply. `elspeth-d1feee1e67`'s comment records the negative-control run and the ArrowDown parking. `elspeth-0bfd019f68` closes (its `composer_tier_default` half was Wave 1's). The epic `elspeth-cd8abcba3f` stays open only if the Roadmap items below are ticketed as children; otherwise close it with the wave summary.

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
| `src/api/decodePrimitives.ts` — one leaf module for the eight decoder primitives, error prefix injected | **Parked with a ticket; named as a known duplication, not presented as clean** | `preferencesDecoder.ts` (Task 10) copies `guidedDecoder.ts:102-190`'s private primitives — the same archetype as the Task 3 topology defect, caught by the structural sweep. Not fixed in Wave 3 because the primitives are called 487 times inside a 2270-line validation module and `invalid` hardcodes the "guided response" prefix in its body, so the extraction is a factory-or-thread-a-prefix refactor of the decoder, not a move. Exporting them unchanged would be worse than the copy — the preferences decoder would report "Invalid guided response at show_advanced". File under the epic; the fix is a leaf module with `prefix` as a parameter and both decoders importing it. |
| Narrow `types/index.ts:180-181,191` (`policy`, `merge`, `scope_policy`) from `string \| null` to the backend Literals | **Recommended follow-up, not a Wave 3 blocker** | Task 4 closes the `policy`/`merge` PHRASE maps against `lib/graphTopology`'s member tuples and `output_mode` against the union `types/index.ts:183` already declares, which gets the compile-time guard for one line and no other file touched. Narrowing the wire types themselves is the fuller fix — it would delete the runtime `titleCaseLabel` fallback as unreachable — but it also touches `guidedDecoder.ts` and every composition fixture, so it is its own lane. `scope_policy` needs a backend Literal to exist first: there is none today. |
| `bind_source` as a reader-register phrase (named beside `require_all` in the `elspeth-d74ab492dd` roadmap row) | **Parked, with the reason the row's premise was wrong** | The row lists `bind_source` as a policy enum to phrase alongside `require_all`. It is not one. `bind_source` has ZERO hits in the frontend and is a backend OPTION VALUE — `options["mode"] == "bind_source"` (`web/composer/yaml_generator.py:94-95`, `pipeline_proposal.py:568`) — so it never reaches `PipelineSpecView`'s routing projection at all; it renders, if anywhere, through `OptionRows`, which this wave does not touch and which Wave 1 ruled renders user-keyed option content verbatim. Phrasing it means deciding whether option VALUES join the register rule, which is a wider decision than one enum and belongs in its own ticket. The `require_all` half of that row IS closed (Task 4's `POLICY_PHRASES`/`SCOPE_POLICY_PHRASES`). File the option-value question under the epic. |
| Graph a11y list item text `transform: classify (llm_transform)` (`GraphView.tsx:1804`) | **Observed, new ticket** | The list is an accessible-name surface (screen-reader text alternative for the diagram) that reads node id + raw plugin id; the reader-register form is `humanNodeType: <step label> (<plugin display name>)`. Out of Wave 3's six rows; file as a register ticket under the epic. |
| Arrow-key (roving tabindex) navigation in the graph a11y list | **New enhancement ticket** | The `elspeth-d1feee1e67` text assumed ArrowDown works; it does not (plain buttons). A roving-tabindex list is a parity change across the a11y suite; Task 7 pins Tab/Enter, which is what exists. |
| Closed-`<details>` aria-describedby AT behaviour | **Manual check in Task 11 Step 4.8** | Cannot be pinned in jsdom; outcome recorded in the epic comment, ticket only if not announced. |

**Sequencing rule (all waves):** one PR per ticket; each PR's default-DOM regression pin (`expectNoIdentifiersInDefaultDom`) is the acceptance test the reviewer runs first.
