---
title: Retire guided as a user-facing mode while keeping the machinery the tutorial runs on
labels: [area/composer, type/task]
---

Guided is being retired as a concept a user can choose, but its implementation stays: the tutorial continues to run on it. This task covers the vocabulary and the user-facing surfaces only.

## Scope

- The Guided radio in Composer preferences is already disabled (`cebd2f263`). Decide whether it is now removed outright, and what a saved Guided default should do for an existing user who has one.
- Remove guided from the user-facing vocabulary: mode copy, documentation, onboarding text, and the "Re-enter guided mode" command-palette entry.
- Guided stops being a mode a user can select. The tutorial keeps running on the guided machinery.

## Explicitly out of scope

Removing the guided implementation. `src/elspeth/web/composer/guided/`, the guided session modules under `src/elspeth/web/sessions/` (including `_guided_step_chat.py` and the guided session routes), and the frontend guided components all stay. The tutorial rides on them.

## Open guided defects stay open

Retiring the mode does not retire the code paths, so the known guided defects remain reachable through the tutorial and must not be closed as part of this work. They include:

- Guided step order inverted — its canonical case is a web-scrape node needing a URL column, which is the tutorial's own pipeline.
- Forking from a guided-recorded correction always failing with an integrity error.
- A guided pending-proposal anchor stranded by checkpoint writers.
- A POST to the guided plan endpoint leaving two pending proposals and breaking the subsequent GET of the guided state.
- Two guided authoring intent-constraint defects, which need a reachability check against the frozen tutorial script before anyone judges whether they still bite.
- A re-run of the tutorial ledger alongside a freeform control.

## Open question, separate work

Whether to migrate the tutorial to freeform. The freeform control run above is the measurement that decides it. Until that is settled, the composer-invariant requirement for a parity sweep across guided surfaces stays in force.
