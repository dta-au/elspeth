---
title: Retire guided as a user-facing mode while keeping the machinery the tutorial runs on
labels: [area/composer, type/task]
---

The Composer offers two ways of authoring a pipeline: *freeform*, a chat with the planner, and *guided*, an ordered stepper that walks source, sink, transforms and wiring. Both talk to the same planner and produce the same pipelines (`docs/guides/user-manual.md`, "Guided and freeform differ in interaction, not in capability"). Freeform is the only mode the project intends to support going forward, but the built-in tutorial is still built on the guided stepper. This task removes guided as something a user can choose, without removing the code the tutorial needs.

**Where this lives.** Mostly the frontend: `src/elspeth/web/frontend/src/components/settings/ComposerPreferencesPanel.tsx` for the mode preference and `src/elspeth/web/frontend/src/components/common/CommandPalette.tsx:161` for the "Re-enter guided mode" entry, plus the user-facing copy in `docs/guides/`. `git show cebd2f263` shows the preference surface and the reasoning behind its current state.

## Scope

- The Guided radio in Composer preferences is already disabled rather than removed (`cebd2f263`), so a user who saved Guided as their default still sees it selected and can switch away. Decide whether it now goes entirely, and what happens to that saved preference.
- Remove guided from the user-facing vocabulary: mode copy in the UI, the user guides, onboarding text, and the "Re-enter guided mode" command-palette entry.
- Guided stops being a mode a user can select. The tutorial keeps running on the guided stepper.

## Explicitly out of scope

Removing the guided implementation. `src/elspeth/web/composer/guided/`, the guided session modules under `src/elspeth/web/sessions/` (including `_guided_step_chat.py` and the guided session routes), and the frontend guided components all stay, because the tutorial runs on them. Known guided defects also stay open and are tracked separately — they are still reachable through the tutorial, so closing them as part of this retirement would lose real bugs.

## Fix

Done means:

- No user-facing surface offers guided as a choice: no radio, no command-palette entry, no documentation telling a user to pick it.
- A user whose saved preference was Guided lands somewhere sensible on next login, with whatever that is decided deliberately rather than falling out of the code.
- The tutorial still completes end to end, which is the check that the retirement stayed on the vocabulary and did not reach the machinery.

Two things need deciding before anyone starts, and neither is a coding question:

1. Whether the disabled radio is removed outright or stays disabled, and what a saved Guided default becomes.
2. Whether the tutorial is eventually migrated to freeform. That decision is not part of this ticket, but it determines how much guided code has to keep working, so it is worth settling first. Until it is settled, changes to the Composer's node-kind handling still have to be applied across the guided surfaces as well as the freeform ones.

Size: small once the two decisions above are made — a frontend and documentation sweep, no backend change. Unstarted, it is decision-blocked rather than large.
