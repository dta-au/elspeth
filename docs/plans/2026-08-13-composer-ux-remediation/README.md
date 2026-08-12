# Composer workspace UX remediation — 2026-08-13

Deliberate pass over the 1080p responsive recode's "janky" state.
Findings ticket: `elspeth-8fa71e6d15` (closed 2026-08-13). Program plan:
`elspeth-ce57c61a4f` (phases: design decisions → chrome conformance →
authoring column & stepper → verification). Landed on `release/0.7.2`
as 6ace0c886..0012544a9.

- `findings.md` — the five root causes and the oracle gap
- `spec-authoring-column-ia.md` — column content per mode/state; pane
  bounds deliberately unchanged (360px is the shipped default at
  1280–1535px viewports; content reduction, not width, was the fix)
- `spec-stepper-visual-language.md` — stepper states with computed WCAG
  1.4.11 verification in both themes
- `chrome-conformance-inventory.md` — the 217-control sweep, target
  classes (.artifact-tab, .link-button), header-height decision,
  per-pinned-test safety table

Deferred (flagged, not lost): collapse-control relocation into the
header actions row (IA spec SHOULD-2), secrets entry → account menu
(SHOULD-3), P3 guided-probe 400→200 (`elspeth-0d8ad56083`). Spin-off
defect: tutorial-completion 429 (`elspeth-06fec73e33`).
