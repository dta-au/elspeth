# Authoring-Column IA Spec — ELSPETH Web Composer
**Filigree step:** elspeth-749cd31e79
**Reviewer:** ux-theorist (first-principles pass)
**Date:** 2026-08-13
**Scope:** content, grouping, width, and per-state behaviour of the authoring column only. The two-pane chat+artifact architecture, the CompletionBar's three co-equal buttons, and the guided stepper's own visual language (numerals/dots/fill) are explicitly out of scope — named as separate decisions where they interact with column content.

**Scope-boundary correction:** the task brief describes the column as sitting "above a completion bar." Structurally it does not — `CompletionBar` renders inside `workspace-action-bar-slot`, which is a child of `workspace-artifact-pane` (`ComposerWorkspace.tsx:408`), i.e. the *right* pane's footer, not the authoring column's. The two appear at the same visual baseline only because both panes share one grid row of equal height (`workspace.css:1-13`) — a coincidence of layout, not a structural relationship. This matters for Stage 4 #17: "Collapse authoring pane" and CompletionBar's three buttons look like four co-equal actions on one shared footer band, but they belong to different panes and different registers.

---

## Stated product purpose

> "ELSPETH is a pipeline engine for building, validating, running, and auditing LLM/data workflows whose outputs must be reviewed, explained, and reproduced. Two authoring surfaces — version-controlled YAML and the authenticated Web Composer (an LLM tool loop) — target one runtime model... Validation and audit are part of the workflow, not after-the-fact diagnostics." — `AGENTS.md`

> "The web Composer is an authenticated chat-driven authoring surface with two modes sharing one runtime: GUIDED (staged wizard... one decision card at a time, stepper strip) and FREEFORM (conversational; intent bubbles, tool-call disclosures, source summaries)." — task brief, corroborated by `ChatPanel.tsx` structure

Two load-bearing facts follow directly from this: (1) the audience is **mixed technical depth** (government/enterprise, not a power-user SaaS market), so the design mandate is *clarity over density*; (2) **audit and validation are first-class**, which is why tool-call disclosures, source summaries, and an authority (auto-apply) chip already exist in code — those are not decoration, they're the product's actual purpose surfacing in chrome.

---

## Stage 1 — Premises Being Set Aside

| # | Premise | Origin | Status |
|---|---------|--------|--------|
| P1 | The authoring column must display its own copy of session identity (title), independent of the app-level header. | Inherited from the pre-recode single-purpose chat rail; predates the current global `AppHeader` (`components/common/AppHeader.tsx`), which now owns `HeaderSessionSwitcher` + `HeaderVersionSelector` at full page width. | **Reject** — confirmed duplicate, see Stage 4 #1. |
| P2 | Model identity (which LLM is composing) must be permanent, always-rendered header chrome. | `elspeth-e9f7678de8` commit rationale ("an auditability product should name the model... not only in run records") — a real requirement, but not evidence it must be a *persistent, space-competing chip in a 360–640px rail*. | **Relitigate** — requirement is real, form is wrong. See Stage 4 #2. |
| P3 | The guided wizard's chrome (header identity row, stepper) should mirror freeform's chat-panel header 1:1. | `ChatPanel.tsx:2528` comment: "Header — mirrors the freeform body header so the mode-switch control... sits in the same top-right spot." A *navigational* symmetry goal, generalised into a *content* symmetry the code only partially honours (guided already omits `AuthorityChip` — see P5). | **Relitigate** — symmetry of the mode-switch control is sound; symmetry of the rest of the header is not derived from anything. |
| P4 | The chat input's rare actions (file manager, secrets) belong in the same persistent 44px-button row as Send, at every width. | Borrowed from feature-rich chat apps (Slack-style toolbar) that assume abundant width; the product already has its own overflow idiom (`WorkspaceActionBar`'s "More actions", `workspace.css:150-171`) that this row does not use. | **Reject** — see Stage 3 and Stage 4 #12/#14. |
| P5 | The guided wizard's authoring surface should bottom-anchor sparse content the way a chat transcript does, so the composer always "hugs" the input. | `guided.css:133-139` comment: "Chat-style bottom anchoring... content must hug the composer." Explicit premise, explicitly borrowed from the chat model for a surface that is not chat. | **Reject** — guided is a wizard/form, not a chat log; see Stage 3 and Section 4. |
| P6 | "Collapse authoring pane" is column chrome that belongs anchored to the pane's bottom edge, at the same visual baseline as the completion actions across the divider. | Inherited CSS placement (`workspace-collapse-control { align-self: flex-end }`, `workspace.css:173-176`); never a deliberate choice about what "collapse" is (a view-density lever) versus what sits beside it visually (product completion gestures in the *other* pane). | **Relitigate** — control earns its place; placement implies false parity with CompletionBar. See Stage 4 #17. |

---

## Stage 2 — Personas Derived From Purpose

| Persona | Goal at this UI | Needs on screen | Does NOT need |
|---------|------|------------------|---------------|
| **Guided first-time author** (e.g. a policy analyst/data steward with no YAML background) | Answer one decision at a time and watch the pipeline take shape, without needing ELSPETH's schema vocabulary. | The Current Decision card (this *is* the interface for them); a lightweight "where am I" progress signal; a text box for the steps that want prose; visible confirmation that a choice was recorded. | Model identity (irrelevant to their task); the Auto-apply authority framing (guided is a scaffolded, single-path turn sequence with explicit Continue/Confirm at every step — evidenced by the header actually omitting `AuthorityChip`, `ChatPanel.tsx:2532-2550` vs `:2711-2737`); a duplicate session-title readout. |
| **Freeform experienced author** (a data engineer / compliance analyst comfortable with source/transform/sink vocabulary, wants speed) | Describe intent in prose, review the LLM's tool calls and diffs, course-correct rapidly. | Full-height scrollable transcript (bubbles, tool-call disclosures, source summaries — these ARE the audit trail, per product purpose); a genuinely usable chat input; the Auto-apply chip (load-bearing — "the one fact a user must be able to see at a glance," per `chat.css:693`); model identity, but as a fact they check, not stare at. | A five-step stepper (freeform has no wizard steps); a duplicate session-title readout. |
| **Reviewing/auditing author** (either mode, at proposal-ready or run-complete state) | Verify what changed, accept/reject, or move to run/export. | Pending-proposal banner with clear accept/reject; enough transcript context to know *why* a proposal exists. | A prominent input row (secondary now — decision has shifted to the artifact pane's diff/graph/YAML tabs); duplicate completion controls (CompletionBar already lives in the artifact pane, correctly, and is out of scope here). |
| **Returning operator** (switches between saved sessions/versions) | Confirm which session/version they're in, resume work. | This is already served — by `AppHeader`'s `HeaderSessionSwitcher` + `HeaderVersionSelector`, at full page width, no truncation risk. | A second, narrower, truncating copy of the same fact inside the authoring column (see P1). |

### Anti-personas (built for, but evidence-free)

| Anti-persona | Why no evidence | Current chrome built for them |
|---|---|---|
| **The density-maximalist power user** who wants every fact (session name, model, auth mode, step count) visible simultaneously, always, regardless of pane width. | Nothing in `AGENTS.md`'s purpose statement or the ticket supports this; the stated audience is "mixed technical depth," and audit/validation-first products favour legibility over density. This anti-persona is a default inherited from generic SaaS-dashboard convention, not derived from ELSPETH's audience. | The 360–640px column simultaneously renders session title + model chip + auth chip + mode-switch + (guided) stepper in one competing row, which is exactly the failure mode the ticket documents (ellipsis truncation, 90px textarea). |
| **The model-watcher** who needs the LLM identity legible at a glance every second, not on demand. | The product's own CSS comment calls the model chip "provenance metadata, not a control" (`chat.css:651`) — i.e. a fact you *check*, not one that needs live-telemetry treatment. | `ModelChip` is rendered as permanent chrome fighting for the same row as the session title and mode-switch, in both mode headers. |
| **The guided-mode power user who reasons about auto-apply/authority mode** | Guided mode is a scaffolded single-path wizard; the code itself does not render `AuthorityChip` in the guided header (`ChatPanel.tsx:2532-2550`). No evidence this persona exists in guided at all — flagged as a design decision already correctly made, not a gap. | N/A — cited to show the pattern IS partially self-correcting; the header duplication problem (P1/P3) is the part that wasn't. |
| **The stepper-numeral reader** who needs literal step numerals ("Step 3 of 5") memorised. | `guided.css:190` already hides numerals unconditionally at the compact width — the product itself has already partially rejected this anti-persona's need, but kept paying the *width cost* of a 5-box horizontal band without the signal that justified it. | The 5-column stepper grid (`guided.css:28-46`) still reserves the same footprint post-numeral-removal. |

---

## Stage 3 — Conceptual Model Audit

| Persona | Current borrowed model | Mismatch? | Better model |
|---|---|---|---|
| Freeform author | Chat app (header + message list + input row), à la Slack/ChatGPT | **No mismatch.** This is genuinely a conversational LLM tool loop; the chat idiom is correct. | Keep as-is; only the header's *content* (Stage 4) and input row's *icon density* (below) need correction. |
| Guided author | The SAME chat-panel chrome as freeform — same header row shape, same bottom-docked composer, same bottom-anchoring trick for sparse content (`guided.css:133-146`) | **Yes.** Guided is a five-step decision wizard/form, not an open-ended conversation. A wizard's chrome convention is top-anchored (progress → content → next action), not "hug the bottom like a chat log." Forcing wizard content to imitate a chat transcript's bottom-anchor is the direct cause of the ~245px void (ticket #8) — the layout is fighting itself between a top-pinned stepper and a bottom-anchoring scroll trick. | Top-anchored form: stepper (pinned top, unchanged) → Current Decision card → optional escape-hatch text box, flowing downward from the top. Sparse content simply leaves ordinary blank space below, which reads as "room to grow," not a fought-over gap. |
| Session/model identity | "Document title in a workspace tab," duplicated below an already-present app-level title bar (`AppHeader`'s `HeaderSessionSwitcher`) | **Yes.** Two copies of the same fact, one of which (the column copy) is structurally guaranteed to truncate because it competes for space with three other chips in a 360–640px rail, while the other (the app header) has the full viewport width and does not. | Identity chrome belongs in the identity-chrome region. `AppHeader` already owns session + version identity; extend it to be the sole home for session-adjacent facts. The authoring column keeps only *compose-state* chrome (authority, mode). |
| Chat input actions | Generic multi-icon "chat toolbar" (folder + upload + key + textarea + Send, all as equal-weight persistent 44px buttons) | **Yes.** This assumes abundant width; at a 360–420px rail it produces exactly finding #5's textarea collapse. The product already has the correct pattern one component over — `WorkspaceActionBar`'s "More actions" overflow (`workspace.css:150-171`) collapses secondary actions (Import YAML, Catalog) behind a single trigger instead of reserving permanent space for each. | Apply the product's own overflow idiom to ChatInput: one primary action (Upload) stays persistent; rare actions (file manager browse, secrets) fold into a single "More" trigger, matching `.workspace-more-actions` visually and behaviourally. |

---

## Stage 4 — Surface-by-Surface Adjudication

Columns: **Guided** / **Freeform** give the mode-specific verdict where they differ; **State notes** flag empty/mid-conversation/proposal-ready/run-complete differences only where a real difference exists (most elements are state-invariant).

| # | Surface | Guided | Freeform | State notes | Lands in |
|---|---------|--------|----------|--------------|----------|
| 1 | Session title (`activeSessionTitle` in column header) | **Kill** | **Kill** | None — always redundant. | `ChatPanel.tsx:2532-2538` (guided) and `:2711-2716` (freeform); delete unused `.chat-panel-header-title` (`chat.css:715-724`). Fact already served by `AppHeader.tsx:29` (`HeaderSessionSwitcher`). |
| 2 | Model chip (`ModelChip`, "Model: openrouter/a…") | **Reframe — relocate** | **Reframe — relocate** | None. | Move out of `chat-panel-header-actions` (`ChatPanel.tsx:2546`, `:2730`) into `AppHeader.tsx`, alongside `HeaderSessionSwitcher`/`HeaderVersionSelector` — the identity-chrome region has full page width and never truncates. `chat-model-chip` styling (`chat.css:650-670`) moves with it. |
| 3 | Auto-apply / authority chip (`AuthorityChip`) | **N/A — already correctly absent** | **Keep** | None. | Unchanged, `ChatPanel.tsx:2726`. Confirms P5 in Stage 2: guided genuinely doesn't need it. |
| 4 | Mode-switch button (`ModeSwitchButton`) | **Keep** | **Keep** | None. | Unchanged, `ChatPanel.tsx:2547`, `:2731-2735`. Core navigational control, low frequency, must stay reachable. |
| 5 | Guided 5-step stepper | **Keep (structurally); width freed for its own visual pass** | N/A | At run-complete (`.chat-panel--completed`), consider collapsing to a compact "Ready" line rather than 5 boxes — flagged as an open question, not decided here (visual-language pass owns it). | `guided.css:112-116` (band), `:24-92` (list/step/index). Numeral/fill treatment stays out of this spec's scope per the ticket's own deliberate-pass split — but removing items #1/#2 from the header frees ~140-180px this pass can now spend. |
| 6 | Current Decision card | **Keep — primary content** | N/A | None. | `guided.css:94-100`. This *is* the guided interface. |
| 7 | Message transcript / bubbles | Secondary (audit log of Q&A) | **Keep — primary content** | Grows through mid-conversation; becomes read-mostly at proposal-ready/run-complete. | Unchanged. |
| 8 | "Decisions so far" recap (`GuidedHistory`) | **N/A — already correctly excluded from the column** | N/A | — | Confirmed already routed to the Inspector's History tab per `ChatPanel.tsx:2564` comment ("Resolved GuidedHistory lives in the common Inspector History tab"). Cited to show not everything here is broken — this relocation was already done correctly. |
| 9 | Tool-call disclosure ("Tool calls (7)") | Keep | **Keep** | Collapsed-by-default at all states; this pattern is correct progressive disclosure. | Unchanged. Directly serves the audit-first purpose. |
| 10 | Source summary card ("Sources (1)", contents, Edit/audit info) | Keep | **Keep** | None. | Unchanged. Compose-state substance both personas need to verify bound data. |
| 11 | Chat input textarea | **Keep, needs min-width floor** | **Keep, needs min-width floor** | None (state-invariant; disabled/read-only variants unaffected). | `ChatInput.tsx:375-388`; `chat.css:460-473` (`.chat-input-textarea`) gains an explicit `min-width`. See Section 3 (width). |
| 12 | File-manager toggle (folder icon) | **Reframe — fold into overflow** | **Reframe — fold into overflow** | None. | `ChatInput.tsx:391-401`; new overflow trigger reuses `.workspace-more-actions` idiom (`workspace.css:150-171`). |
| 13 | Upload icon button | **Keep, persistent** | **Keep, persistent** | None. | `ChatInput.tsx:404-431`. Directly tied to the core "give the composer your data" action (also named in the empty-state placeholder copy, `ChatInput.tsx:327`). |
| 14 | Secrets/key icon button | **Reframe — fold into overflow (this pass); flag Account/UserMenu as future home** | **Reframe — fold into overflow (this pass)** | None. | `ChatInput.tsx:434-445`. Minimum-viable fix: same overflow trigger as #12. SHOULD-not-MUST: relocate to `UserMenu` (`AppHeader.tsx:34`) long-term, since secrets are account/session config, not a per-message action — bigger relocation, out of MUST scope this pass. |
| 15 | Send button | **Keep** | **Keep** | None. | Unchanged. Primary action. |
| 16 | "Shift+Enter for new line" hint | Keep | **Keep** | None. | Unchanged, low-cost caption. |
| 17 | "Collapse authoring pane" control | **Reframe — reposition** | **Reframe — reposition** | None. | Move out of the pane-bottom `align-self: flex-end` slot (`workspace.css:173-176`, `ComposerWorkspace.tsx:333-341`) into the header-actions row beside Mode-switch, as a compact icon control. Decouples it visually from the artifact pane's Validation/Audit chips + CompletionBar, which sit at the same baseline today and imply false parity between a view-density toggle and product completion gestures. |
| 18 | Collapsed-state restore affordance | Keep | **Keep** | None. | Unchanged, `ComposerWorkspace.tsx:343-361`. Works, serves its purpose. |

**Killed:** #1 (session title, both modes).
**Reframed:** #2 (model chip → AppHeader), #5 (stepper → width freed, visual pass separate), #12/#14 (rare icons → overflow), #17 (collapse control → header row).
**Confirmed already correct (no action):** #3 (authority chip guided-omission), #8 (GuidedHistory → Inspector).

---

## Section 3 — Width Decision

**Recommendation: do not change the pane bounds.** `AUTHORING_MIN = 360`, `AUTHORING_MAX = 640`, `ARTIFACT_MIN = 640`, `STANDARD_DESKTOP_MIN = 1536` (`useWorkspacePaneState.ts:21-24`) are sufficient once content is reduced per Stage 4 — the defect is content overload, not a wrong number.

**360px is not an edge case — it is the shipped default at the task's stated minimum supported width.** `paneBoundsForWidth` sets `defaultWidth = measuredWidth < STANDARD_DESKTOP_MIN(1536) ? AUTHORING_MIN : 420` (`useWorkspacePaneState.ts:118`). At the constraint brief's own "minimum supported width 1280px," that evaluates to `AUTHORING_MIN = 360`. So 360px is not something a user must drag the separator or carry a stale persisted preference to reach — it is the out-of-the-box column width for every user at the minimum supported width. (It remains reachable at any wider viewport too, by drag or by a persisted `preferredAuthoringWidth`, `readStoredWorkspaceLayout` in the same file, which is not re-derived from the current viewport.) Design for 360px as the baseline case, not the corner case — this is *why* content reduction, not width tuning, is the correct fix.

### Arithmetic — chat-input row at the 360px floor

`.chat-input` padding is `var(--space-sm) var(--space-lg)` → 16px horizontal each side = 32px total (`chat.css:419-422`). `.chat-input-row` gap is `var(--space-sm)` = 8px between every item (`chat.css:454-458`). The textarea has no `min-width` (`.chat-input-textarea { flex: 1 }`, `chat.css:460-461`) — it absorbs whatever space fixed siblings leave.

**Today, non-composing** (`disabled=false`, `onCancel` block hidden): 4 fixed controls — folder(44) + upload(44) + key(44) + Send(44) = 176px. 5 items in the row → 4 gaps × 8px = 32px.

```
176 (controls) + 32 (gaps) + 32 (padding) = 240px fixed
At 360px: textarea = 360 − 240 = 120px
```

**Today, composing** (`disabled=true`): Stop is *additive*, not a swap — `ChatInput.tsx:447-456` renders it before Send, which still renders unconditionally afterward (`chat.css:515-518` gives Stop `min-width: 56px`). The icon buttons are gated on `!readOnly`, not on `disabled`, so they stay too. Worst case is 5 fixed controls: folder(44) + upload(44) + key(44) + Stop(56) + Send(44) = 232px. 6 items → 5 gaps × 8px = 40px.

```
232 (controls) + 40 (gaps) + 32 (padding) = 304px fixed
At 360px: textarea = 360 − 304 = 56px
```

This — not the non-composing 120px — is the true worst case, and it occurs at the shipped default for every 1280–1535px-wide session.

**After Stage 4** (#12/#14 folded into one overflow trigger, "More"): row becomes textarea + Upload + More + (Stop) + Send.

*Non-composing:* Upload(44) + More(44) + Send(44) = 132px. 4 items → 3 gaps × 8px = 24px.
```
132 + 24 + 32 = 188px fixed → at 360px: textarea = 360 − 188 = 172px
at 420px (default at ≥1536px workspace): textarea = 420 − 188 = 232px
```

*Composing:* Upload(44) + More(44) + Stop(56) + Send(44) = 188px. 5 items → 4 gaps × 8px = 32px.
```
188 + 32 + 32 = 252px fixed → at 360px: textarea = 360 − 252 = 108px
```

108px is still below the 140–160px floor this section recommends. Reducing the icon count from 4 to 3 is necessary but not sufficient at the composing worst case — a wrap rule is required, not optional.

### MUST: wrap rule (not merely flagged)

Upload and More wrap to their own row **below** the textarea/Stop/Send row whenever the primary row's textarea would otherwise drop under its floor. Stop and Send never wrap — they track the live send/cancel action the user's attention is actually on at that moment, and give up the most when width is scarce is the pair the user isn't using *right now* (Upload, More), not the pair reflecting current state (Send/Stop). Re-run the arithmetic with that split:

```
Non-composing wrapped row: textarea + Send(44) → 1 gap(8) + 44 + 32(padding) = 84px fixed
  At 360px: textarea = 276px
Composing wrapped row: textarea + Stop(56) + Send(44) → 2 gaps(16) + 100 + 32(padding) = 148px fixed
  At 360px: textarea = 212px
```

Both clear the 140–160px floor with margin. At 360px — the default at minimum supported width — Upload/More should wrap by default, not only past some rare narrower threshold.

### Recommended values

| Token/rule | Value | Rationale |
|---|---|---|
| `AUTHORING_MIN` (`useWorkspacePaneState.ts:21`) | 360px — **unchanged** | Sufficient once the row drops to 3 persistent controls plus the mandatory wrap rule above. |
| `AUTHORING_MAX` (`useWorkspacePaneState.ts:22`) | 640px — **unchanged** | Not implicated by any finding. |
| Default authoring width at ≥1536px workspace (`useWorkspacePaneState.ts:118`) | 420px — **unchanged** | Already correct; the ticket's screenshots were captured near this value and still broke, confirming the fix is content, not width. |
| `.chat-input-textarea` `min-width` (`chat.css:460-473`) | ~140-160px (new) | Hard floor; the wrap rule above is what actually keeps the textarea at or above it, not the min-width alone — the min-width is the backstop that makes a wrap-rule bug visible (textarea pinned at floor and clipping) instead of silently collapsing further. |

### Implementation note (technique, not value)

The existing narrow-mode treatment (`chat.css:564-601`, `@media (max-width: 760px)`) keys off **viewport** width. The authoring column's width is independent of the viewport — a 1920px viewport can host a 360px pane via the resizable separator, and 1280–1535px viewports ship 360px by default (see above). A `max-width` media query cannot see either case. The MUST wrap rule above should key off the **pane's own width**, which likely means a CSS container query (`container-type: inline-size` on `.workspace-authoring-pane` or `.chat-input`) rather than, or in addition to, the viewport query. This is a technique recommendation for the implementer to weigh, not a value this spec fixes.

---

## Section 4 — Dead-Space Resolution (sparse guided sessions)

**Diagnosis (Stage 3):** the ~245px void (ticket #8) exists because the layout tries to honour two anchor models at once — a top-pinned stepper (correct for a wizard) and a bottom-anchoring scroll trick borrowed from chat (`guided-authoring-scroll > :first-child { margin-top: auto; }`, `guided.css:144-146`, explicitly commented as "chat-style bottom anchoring"). Guided mode is a form/wizard (Stage 3), not a chat log — it should not fight to imitate one.

**Resolution — this relocates the void, it does not eliminate it, and that relocation is the actual fix.** Remove the bottom-anchoring rule (`guided.css:144-146`). Let the scroll region's content top-anchor naturally: stepper (pinned, top) → Current Decision card → any accumulated history, flowing top-down. The persistent, bottom-docked composer input (`ChatInput`, rendered as a sibling after `.guided-authoring-scroll`) stays exactly where it is — same physical screen position as freeform's input, preserving muscle memory across mode switches (P3's *navigational* symmetry is worth keeping even though header *content* symmetry is not, see Stage 1).

Today the gap sits *above* the card — between the stepper (empty capture, `y≈117`) and the card (`y≈360`) — because the auto-margin pushes the card down to hug the composer. After removing the rule, a gap of roughly the same magnitude reappears *below* the card, between the card's bottom edge and the docked input. The total blank pixels are not smaller. What changes is what the gap is adjacent to: today it sits between two pieces of navigational/decision chrome (stepper, card) with nothing visually explaining why they're separated; after the fix it sits between the decision content and the input strip, which is exactly where a short form's trailing whitespace is expected to be — the same shape every step-by-step form takes when its content doesn't fill the viewport. No filler content, no forced growth, and no structural change to where `ChatInput` mounts in the component tree. State this to the operator as "relocated to the position where it reads as normal," not "removed."

**If the operator wants the space actually reclaimed** rather than relocated: move the free-text escape-hatch input *inside* `.guided-authoring-scroll` as its last child (so it scrolls with content instead of docking), matching a literal form's "field → next-field → submit" flow. This is the option that eliminates the gap rather than repositioning it, at the cost of a larger structural change (moves `ChatInput`'s mount point, breaks the guided/freeform input-position symmetry named above). Offered as the fallback because it costs more, not because it's less correct — if the relocated gap still reads as odd in visual QA, this is the next lever, not a lesser one.

---

## Section 5 — Named Design Tensions and Open Questions

| Tension | Persona A | Persona B | Resolution strategy |
|---|---|---|---|
| Identity chrome vs. compose-state chrome | Returning operator wants session/model identity readable, anywhere, without hunting | Active author (either mode) wants the narrow column spent entirely on decision content and input, not identity readouts | **Surface separation** — identity facts move to `AppHeader` (full width, never competes with compose content); the column keeps only what changes turn-to-turn (authority, mode, decision content). Cheapest strategy that actually resolves this — not progressive disclosure, because the two personas want the SAME fact at DIFFERENT times/places, not the same fact at different depths. |
| Guided (form) vs. freeform (chat) needing different anchor models in one shared component family | Guided novice wants a top-anchored, form-shaped decision surface | Freeform author wants a bottom-anchored, chat-shaped transcript | **Mode switching** — already structurally present (`.chat-panel--guided` vs. the freeform branch are separate render paths in `ChatPanel.tsx`); Section 4's fix makes the *anchor behaviour*, not just the *chrome*, mode-specific. Not resolvable by progressive disclosure — the two goals are structurally different, not the same goal at different depths. |
| Rare-action discoverability vs. persistent-row crowding | A user who needs secrets/file-manager wants them visible without hunting | Everyone else needs the textarea usable | **Progressive disclosure via the product's own established overflow idiom** (`.workspace-more-actions`) — correctly applicable here because the underlying goal (act on this pipeline/session) is shared; only frequency differs. This is the one tension where progressive disclosure is the right tool, per the protocol's own guidance that it works for shared-goal/different-depth, not different-goal situations. |

### Open questions for the operator

1. **Secrets icon's long-term home.** This spec recommends folding it into a "More" overflow trigger as the minimum viable fix, but flags relocating it to `UserMenu`/account settings as a stronger long-term fit (secrets are account-level config, not a per-message compose action). Confirm which is intended for this pass vs. deferred.
2. **Stepper visual language at run-complete.** Should the 5-step band collapse to a compact "Ready" summary once `.chat-panel--completed`, or stay as-is? Flagged, not decided — the stepper's internal visual language is the ticket's own separately-scoped deliberate pass.
3. **Freeform empty state.** No screenshot evidence was available for freeform's empty state (only guided-empty was captured). This spec cannot verify whether freeform's empty state has its own version of the "void" problem or needs its own onboarding content — flagged as an evidence gap, not silently assumed clean.
4. **Model chip in `AppHeader`.** Relocating it there assumes `AppHeader` has width budget at the 1280px minimum-supported width once it already carries WordMark + SessionSwitcher + VersionSelector + UserMenu. This spec did not audit `AppHeader`'s own responsive behaviour — the implementer should confirm it doesn't inherit the same truncation problem it's being asked to solve.

---

## Derived UX Requirements

**MUST do** (failing this means the product fails its purpose for a derived persona):
1. Remove the duplicate in-column session title (Stage 4 #1) — it serves no persona and is the clearest instance of premise drift (P1).
2. Give `.chat-input-textarea` an explicit `min-width` floor, reduce the persistent icon-button count from 4 to 3 (Upload, More, Send), **and** implement the wrap rule (Upload/More drop to a row below textarea/Stop/Send) — the icon-count reduction alone still collapses the textarea to 108px in the composing state at the 360px default for 1280–1535px viewports; the wrap rule is what actually holds the floor (Section 3).
3. Remove the bottom-anchoring rule for sparse guided content (`guided.css:144-146`). This relocates the void to below the Current Decision card rather than eliminating it — state that to the operator as the intended outcome, not a partial fix (Section 4).
4. Preserve `AuthorityChip` in freeform's header — it is the one fact this audit-first product's own code says must always be visible at a glance; do not fold it into any overflow.

**SHOULD do** (meaningfully better, not load-bearing):
1. Relocate `ModelChip` to `AppHeader`, freeing header width for guided's stepper and simplifying freeform's header to Authority + Mode-switch only.
2. Reposition "Collapse authoring pane" into the header-actions row so it stops visually implying parity with CompletionBar's product-completion buttons across the pane boundary.
3. Move the secrets icon to `UserMenu`/account settings rather than the compose-row overflow, once the minimum-viable overflow fix has shipped and been observed in use.
4. Implement the MUST wrap rule (item 2 above) via a container query rather than a viewport media query, since the authoring pane's width is independent of viewport width — this is a technique preference, not a second requirement; a viewport-query approximation that happens to cover the 1280–1535px case would satisfy the MUST without it.

**WON'T do** (explicitly out of scope — written down so it doesn't drift back in):
1. Redesign the CompletionBar's three-button layout or emphasis — pinned product intent (`CompletionBar.test.tsx:116-122`).
2. Decide the guided stepper's internal visual differentiation (numerals vs. dots vs. fill) — separately scoped deliberate pass per the ticket.
3. Recut the two-pane chat+artifact architecture — settled per task constraints.
4. Change `AUTHORING_MIN`/`AUTHORING_MAX`/`ARTIFACT_MIN` bounds — content reduction makes the existing bounds sufficient; widening them would be treating a content-overload symptom with more pixels instead of fixing the overload.

---

## SME Protocol

### Confidence Assessment
- **High** — file:line citations for every cited rule/component; the session-title duplication (confirmed against `AppHeader.tsx`); the `AuthorityChip` guided/freeform code split (confirmed by direct read of `ChatPanel.tsx:2532-2550` vs. `:2711-2737`); the width arithmetic (derived from actual token values in `useWorkspacePaneState.ts` and `chat.css`, not estimated).
- **Moderate** — whether guided mode's *backend* semantics ever vary auto-apply/authority in a way the frontend simply doesn't surface; this spec infers "guided doesn't need the chip" from the chip's absence in the guided header render path, not from reading the guided protocol/backend contract.
- **Moderate** — the precise visual outcome of Section 4's fix (removing one CSS rule); reasoned from CSS structure and inline comments, not rendered/screenshotted.
- **Lower / explicitly flagged as inference** — persona and anti-persona characterisations are derived from the codebase's own comments and `AGENTS.md`'s purpose paragraph, not from user research, support tickets, or usability testing artefacts — none exist in this repository per the ticket's own admission of no 1920×1080 baseline coverage.

### Risk Assessment
- Killing/relocating session title and model chip touches components with existing pinned tests (`ChatPanel.test.tsx`, `ModelChip.test.tsx` and siblings) that this spec did not read. The implementer owes the same test-safety verification pass the prior `ux-critic` review performed (reading the pinned assertions before landing) — not assumed clear here.
- Folding secrets out of the primary compose row could reduce discoverability for a security-relevant action for a first-time user; this is presented as a SHOULD-tier call precisely so the operator can veto or downgrade it rather than have it land silently as a MUST.
- The stepper's freed width (Stage 4 #5) is a byproduct of removing #1/#2, not a redesign of the stepper itself — implementers should not read "width freed" as license to also change the stepper's visual language without the separately-scoped pass the ticket named.
- Relocating `ModelChip` to `AppHeader` assumes spare width there; unverified (Open Question 4).

### Information Gaps
- No freeform empty-state screenshot was available — Open Question 3 names this explicitly rather than assuming freeform's empty state is clean.
- `ModelChip.tsx`, `AuthorityChip.tsx`, and `GuidedWorkflowStepper.tsx` source files were not read directly; conclusions about their behaviour come from call sites and CSS, which is sufficient for an IA-content spec but should be re-verified by whoever implements the relocation.
- No user-research or support-ticket corpus exists in this repository to validate persona claims beyond the product's own internal design-intent comments — flagged per protocol rather than presented as user-tested fact.
- Dark theme was not captured in the evidence screenshots (light theme only, per the ticket's own caveat) — this spec's recommendations are theme-agnostic (content/layout, not colour), but visual verification in dark mode remains owed before landing.

### Caveats
- This is IA/content theory, not a wireframe — no pixel positions are specified beyond the arithmetic in Section 3; visual composition (spacing, alignment, exact overflow-trigger iconography) is implementation work, informed by but not fixed by this spec.
- Every "Kill" or "Reframe → relocate" verdict removes DOM/behaviour currently exercised somewhere in the frontend test suite. Per the project's own doctrine ("scoped test runs miss cross-cutting gates"), the implementation phase owes a full test run, not a scoped one, before landing.
- The container-query recommendation in Section 3 is offered as a technique, not a mandate; the implementer should confirm current browser-support posture before committing to it over (or alongside) the existing viewport media query.
