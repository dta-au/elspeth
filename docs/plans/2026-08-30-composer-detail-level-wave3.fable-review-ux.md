> Saved by team-lead from the reviewer's inline return (its file write was harness-blocked). Truncated mid-I-1 at "a routing value's raw connection name, t" — remainder requested via SendMessage; PART 2 appended below when received.

**VERDICT: APPROVE_WITH_CONCERNS**

**Counts:** Critical 0 · Important 4 · Minor 4

- **I-1** — The Advanced toggle does not actually reveal the identifiers this wave hides: Spec-tab `<h4>`/routing `<dd>`s, ModelChip, ScopeBadge, provenance, and DiagnosticValue are `title`-only at *both* detail levels (mouse-only recovery), contradicting the wave premise and the plan's own "`title` alone is not a home" rule.
- **I-2** — `modelDisplayName.ts` mangles Bedrock-form ids: `bedrock/anthropic.claude-3-haiku-20240307-v1:0` renders "Anthropic.claude 3 Haiku 20240307 V1:0" in the header chip and the run-consent reader line.
- **I-3** — Egress dialog: in-flow `.sr-only` identifier sentence makes screen readers read every consent line twice, verbatim, with no framing — remedy is a one-phrase prefix ("exact identifiers: …").
- **I-4** — `specRouting.connectionPhrase` title-cases a dangling connection into the same register as a resolved component ("Then: Nowhere Yet" reads like a real step), so a miswired pipeline looks healthy on the review surface.

Minor: M-1 multiple no-context validation errors all collapse to "this step" (lost disambiguation, inconsistent with `humaniseStepLabel`'s title-case fallback); M-2 two removed nodes are indistinguishable ("Removed step · prompt" twice, raw id only in `data-*`); M-3 blob row/column counts arguably reader-register, over-gated (deliberate per ticket — judgment note); M-4 consent dialog and Spec tab name a described source differently (no description rung in the egress `component()` helper).

---

# UX Designer Review — Composer Detail Level Wave 3 (full report)

Branch `feature/composer-detail-level-wave3`, `7cd2fc6db..8b85a9314`. Full diff read (66 files, 7,179 diff lines); plan sampled for the title-attribute, sr-only, and heading rulings; live files consulted for `titleCaseLabel`, `UNKNOWN_COMPONENT_PHRASE`, and `pluginDisplayName`.

## Verdict: APPROVE_WITH_CONCERNS

The wave is a substantial net improvement in reading experience: the humanized phrases are almost uniformly clearer than the identifiers they replace, the omit-don't-hint gating is disciplined (no teaser text anywhere), honesty rules are consistently applied (unknown enums stay in `<code>`, unparseable blobs keep their caveat at every level, "about N characters" is a truthful bound), and the test discipline around the default-DOM pin is exemplary. The concerns below are real but none is a blocker: the largest is a coherence gap between the wave's stated premise ("Advanced reveals the machine identifiers") and what the Advanced toggle actually reveals.

## Important findings

### I-1. The Advanced toggle does not reveal the identifiers this wave hides — `title` is the only sighted recovery, and it is mouse-only

**Where:** `src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.tsx:118` (`<h4 title={row.id}>`), the routing `<dd>`s via `RoutingDd` (PipelineSpecView.tsx:72-92), `components/chat/ModelChip.tsx:31`, `components/settings/SecretsPanel.tsx` (ScopeBadge), `components/chat/InlineSourceCreatedTurn.tsx:193`, `components/execution/RunsHistoryDrawer.tsx` (`DiagnosticValue`).

**What:** The wave's premise is that `show_advanced` reveals machine identifiers for power users. None of the surfaces above consult `showAdvanced`: at *both* detail levels the raw id/enum lives only in a `title` attribute. The plan's own acceptance rule (plan ~line 2269) states "`title` as a sighted-mouse convenience alongside [a reachable home] rather than instead of them … `title` alone is not a home" — Task 6 (egress) and Task 5 (amendment bytes) honour it with `.sr-only` spans, while these surfaces ship `title`-only.

**User-facing consequence:** A sighted keyboard-only user — and any touch-device user, where `title` never fires — has no route to a Spec card's raw id, a routing value's raw connection name, t
[part 2 received — report complete]

---
## PART 2 (final; continues mid-sentence from "a routing value's raw connection name, t")

he raw model id, the raw secret scope, or a diagnostic enum, at either detail level, except leaving for the YAML tab and correlating by hand. Screen-reader users fare slightly better on the Spec tab only because the card `aria-label` still carries `Node extract_invoice` (deliberately pin-exempted). Answering the brief's direct question: **no, "the Advanced toggle exists" is not a mitigation, because the toggle does not in fact reveal these identifiers.**

**Remedy:** With `show_advanced` on, render the raw token in a `<code>` secondary beside the phrase (the pattern `UnavailableComponentRow` already uses, and a register the pin already exempts). Highest value: the Spec-tab `<h4>`, routing `<dd>`s, and `DiagnosticValue` (a raw reason/cause enum is exactly what gets pasted into a support search). ModelChip and ScopeBadge are lower stakes and can stay `title`-only if adjudicated.

### I-2. `modelDisplayName` mangles dot-separated (Bedrock) model ids

**Where:** `src/elspeth/web/frontend/src/components/chat/modelDisplayName.ts:11-21`; consumed by `ModelChip.tsx` and `ExecuteButton.tsx`'s `model()` helper.

**What:** The derivation is leaf-of-path + hyphen split + title case. It reads well for `openrouter/anthropic/claude-sonnet-4.6` → "Claude Sonnet 4.6". But a Bedrock id — a format this repo's own fixtures use (`bedrock/anthropic.claude-3-haiku-20240307-v1:0`, ExecuteButton.test.tsx) — yields **"Anthropic.claude 3 Haiku 20240307 V1:0"**: the dot survives, and the date stamp and version suffix are title-cased as if they were words. Neither clean prose nor the recoverable raw id — worse than both.

**User-facing consequence:** In an AWS deployment (a supported target — the ECS/Bedrock ops tooling exists), the header chip reads "Composer: Anthropic.claude 3 Haiku 20240307 V1:0", and the consent dialog's reader line shows the same mangling with "via bedrock" appended. On a consent surface a garbled model name actively undermines the trust the phrasing is meant to build.

**Remedy:** Guard the derivation: phrase the leaf only when it matches a wordish shape (no internal letter-adjacent dots, no long digit runs); otherwise return the raw leaf/id unchanged — an honest identifier beats a fake name, which is this wave's own doctrine for unknown enums (`diagnosticPhrases.ts`: "an unknown identifier must never be dressed up as a sentence").

### I-3. Egress dialog: screen readers hear every line twice, verbatim, with no framing

**Where:** `src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.tsx`, egress list render: `<li title={identifiers}>{text}<span className="sr-only">{identifiers}</span></li>`.

**What:** Removing `aria-describedby` (the double-announcement fix) was right, but the in-flow `.sr-only` span still makes the `<li>`'s accessible content "Reads source data: Source (CSV). Reads source data: source (csv)." — and case and parentheses do not survive speech, so a screen-reader user hears the same sentence read twice, back to back, on every line of a consent dialog. Four egress lines become eight sentences; it presents as a stutter or rendering bug, with no cue explaining the repetition.

**User-facing consequence:** The audience the `.sr-only` span exists for gets the most degraded reading of the dialog; on a consent surface, confusion reads as untrustworthiness.

**Remedy:** Prefix the hidden span: `<span className="sr-only"> (exact identifiers: {line.identifiers})</span>`. One phrase turns apparent duplication into an intelligible disclosure, keeps every sentence (the R2-F7 obligation), and needs only `toHaveTextContent` updates in the tests.

### I-4. A dangling connection is indistinguishable from a healthy resolved one

**Where:** `src/elspeth/web/frontend/src/components/workspace/specRouting.ts`, `connectionPhrase` — unresolved connections fall back to `titleCaseLabel(connection)`.

**What:** A resolved routing value renders the far component's phrase ("Then: Results"); a connection with no consumer/producer renders its own name title-cased ("Then: Nowhere Yet"). Both are Title Case prose with the raw value in `title` — same register, same shape. Pre-wave, a dangling value at least *looked* like a machine string among labels.

**User-facing consequence:** The Spec tab is a review-and-trust surface, and this wave's premise is that non-engineers review here rather than in YAML. A mid-edit or misconfigured pipeline whose `on_success` points nowhere now reads exactly like a wired one — "Nowhere Yet" looks like a step named "Nowhere Yet". Validation flags it elsewhere, but the card itself asserts a connection that does not exist. (Same pattern in the coalesce branch prose: an unwired branch reads "Branch B → Hex Done" as if "Hex Done" were a real step.)

**Remedy:** Mark the unresolved arm — e.g. "Nowhere Yet (not connected)", or render the raw name in `<code>` for the dangling case only. `connectionPhrase` already knows resolution failed (the `undefined`/empty arm), so this is a one-branch change plus test updates.

## Minor findings

### M-1. Multiple validation errors without node context all collapse to "this step"

`ValidationResult.tsx`, `resolveComponentName` — the `!nodes` arm now returns `phraseFor(componentId)`, whose no-context fallback is `UNKNOWN_COMPONENT_PHRASE` ("this step"). Two errors on two different components render identically; pre-wave the raw ids were ugly but distinct. Also inconsistent with `humaniseStepLabel`, which title-cases the id when the composition is merely unloaded (`interpretationStepLabel.ts`). Remedy: adopt the same ladder — `titleCaseLabel(componentId)` when no context — preserving disambiguation without leaking the raw id. Recovery today is the "Technical details" disclosure, so Minor.

### M-2. Two removed nodes are indistinguishable ("Removed step · prompt" twice)

`interpretationStepLabel.ts` + `AcknowledgementStack`: the raw id lives only in `data-affected-node-id`, which the plan itself calls a forensic home invisible to every audience. Two acknowledgement cards referencing different deleted nodes carry identical titles with no visible or hoverable disambiguator at either detail level. Remedy: `title` with the raw id on the card heading, or "Removed step (was Extract Invoice)" — the ghost id was the author's own name, so title-casing it is not an identifier leak under this wave's own author-name rule.

### M-3. Blob row/column counts may be over-gated

`BlobRow.tsx`: "2 rows · columns: name, age" was classified engineer-register and gated behind Advanced (deliberate per elspeth-f1394307e3 — this is a judgment note, not a defect report). But that line is plain language a non-engineer understands and is precisely the reassurance a user wants after uploading a file ("did it read my spreadsheet right?"). The caveat-stays/counts-go split keeps honesty but removes comfort. Worth revisiting with confusion telemetry rather than on principle.

### M-4. The consent dialog and the Spec tab can name the same source differently

`ExecuteButton.tsx`'s `component()` resolves via `stepLabelForNodeId ?? titleCaseLabel` (no description rung for sources/outputs), while `specRouting.componentPhrase` and `validationHumaniser.makePhraseFor` overlay descriptions for all three component kinds — a divergence `specRouting.ts`'s own docblock warns about. A source described "Quarterly invoices from finance" reads that way on the Spec tab and in validation prose, but "Intake (CSV)" on the run-consent dialog. The register rule ("same concept, same phrase, every surface") breaks on the surface where recognition matters most. Remedy: route the egress `component()` helper through the shared description-first ladder, keeping the identifier register untouched.

## Notably good (brief)

- Honesty discipline is consistent and tested: unknown diagnostic enums stay `<code>`; unparseable blob bodies keep their caveat at every level; the Removed / present-but-unlabelable / unloaded three-way split in `humaniseStepLabel` is exactly right.
- The Reply Register prompt section anticipates the vagueness failure mode ("'There were some issues' is a worse reply than `is_valid: false`") and the prompt test pins that clause specifically.
- The amendment cap warning is a model conversion: actionable sentence first, exact figures reachable by every audience inside the live region.
- The egress reader/identifier alignment guard (throw on length mismatch) converts a prose claim into a checkable invariant on an audit-critical surface.
- The e2e keyboard spec honestly states its coverage gaps, and the computed-`clip` assertion is genuinely load-bearing where geometry is blind.

## Confidence

**High** on I-1, I-3, M-1, M-2 (read directly from shipped code and the plan's own rulings; behaviour is deterministic). **High** on I-2's mechanics (traced through `titleCaseLabel`); **medium** on its severity, which depends on Bedrock-form composer/model ids occurring in real deployments (fixtures and the AWS ops tooling suggest yes). **Medium** on I-4 and M-4: real on the code as written, but I did not run the UI, and the frequency of dangling connections / described sources in real sessions is inferred, not measured.

## Information Gaps

- I did not run the app or the test suites; all findings are from static reading of the diff, plan, and live files.
- I could not check tickets (elspeth-93f5621f18 / elspeth-d74ab492dd) for an explicit adjudication that `title`-only is acceptable on the Task 4/6 surfaces; the plan text I found rules the opposite ("title alone is not a home", plan ~line 2269) while its own per-item code blocks ship `title`-only, so I-1 may partly be a plan-internal inconsistency already argued elsewhere.
- Screen-reader behaviour in I-3 is asserted from the accessibility-tree content model (in-flow `.sr-only` text is read as content by all major SRs); not verified against a live NVDA/VoiceOver session.
- Whether `ELSPETH_WEB__COMPOSER_MODEL` is ever a Bedrock-form id in a real deployment (bears on I-2's reach).

END OF REPORT.
