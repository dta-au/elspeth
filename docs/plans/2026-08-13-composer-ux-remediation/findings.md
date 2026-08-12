UX review (2026-08-13, live captures at 1920x1080 and 1920x930 against the local
deployment, lyra-ux-designer:ux-critic pass over the Aug 8-11 responsive recode
commits eebe521aa..29fd18590) found the composer workspace "jank" decomposes into
five root causes. Layout is functionally correct; the recode left two visual
languages on screen at once.

ROOT CAUSES
1. Unskinned workspace chrome: WorkspaceActionBar status chips + "More actions",
   "Collapse authoring pane" (ComposerWorkspace.tsx:333), and the artifact tabs +
   "Focus Graph" (ArtifactWorkspace.tsx:313) declare structure-only CSS
   (workspace.css:64-176) and never compose the tokenized .btn/.btn-compact system
   (styles/shared.css:136-222). They sit in the same rows as fully-styled .btn
   siblings — this is most of the perceived jank.
2. Overloaded 360px authoring column: --authoring-pane-width (workspace.css:2)
   inherited from the old single-purpose rail now hosts session header, model chip,
   stepper, decision card, chat input, completion bar. Causes ellipsis truncation
   (chat.css:654-724) and a ~90px chat textarea (chat.css:460-495: flex:1 with no
   min-width vs 3-4 fixed 44px icon buttons).
3. Stepper lost its sequence signal: guided.css:190 unconditionally hides step
   numerals; remaining state differentiation is a 1px border shift, so steps read
   as input boxes.
4. Header halves disagree: .chat-panel-header computes 60px, .artifact-workspace-
   toolbar 52px; compaction media queries only fire below 800px height so 1080p
   gets neither intent. Visible 8px step in the divider.
5. Small graphs float in a huge canvas: GraphView.tsx:717 calls bare
   instance.fitView(), not guaranteed to honour the fitViewOptions (maxZoom 1.5)
   passed at :1654.

CONTRIBUTING GAP: the visual-oracle baseline matrix samples 1280x720, 1536x760,
1920x900, 2560x1280 — there is NO 1920x1080 baseline, which is how the original
1080p break shipped and how this jank survived a green suite.

FLAGGED AS DELIBERATE-PASS SCOPE (not CSS tweaks): what content should live in
the authoring column per mode/step (IA question), and the stepper's visual
language. Both inherited from the wider pre-recode layout and compacted
reactively.

NON-DEFECT: console 400 on GET /api/sessions/<id>/guided for freeform sessions is
an intentional handled probe (sessionStore.ts:942-983); noise is browser-native.
Optional backend nicety: return 200 with guided_session: null.

Full severity table, 8 quick wins with test-safety verification, and landing
order in the first comment. Screenshots archived in session scratchpad
(ux-review/*.png). Remediated by the "Composer workspace UX remediation" plan.
