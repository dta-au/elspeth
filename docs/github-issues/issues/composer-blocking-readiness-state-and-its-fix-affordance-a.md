---
title: Blocking composer readiness state and its fix affordance sit in a sub-tab
labels: [area/composer, area/web, type/bug]
---

When a review gate blocks a composer session from completing, none of the user's
top-level surfaces offered a way to act on it: the only actionable control was two tab
changes away, in Pipeline → Checks. Something that blocks execution should not be reached
through a submenu.

This is frontend work, in `src/elspeth/web/frontend/src/components/`. The relevant server
shape is `ValidationReadiness` (`src/elspeth/web/frontend/src/types/index.ts:614`): three
booleans — `authoring_valid`, `execution_ready`, `completion_ready` — plus a `blockers`
list. "A readiness axis is false" below means any one of those three booleans is false.

## What happens

Measured on a live session on 13 September 2026. The end-of-session advisor gate — the
review step that must sign off before a session can be handed on — returned a terminal
block with static checks otherwise passing, and completion was withheld. At that moment:

- Chat carried a fixed "System note" with no action attached.
- Save-for-review was disabled, with the reason available only as a hover title
  (`src/elspeth/web/frontend/src/components/composer/CompletionBar.tsx:95`, the
  `completionBlockedTitle` branch).
- The Checks tab badge read "1 warning". At that date `projectValidationWorkspaceStatus`
  (`src/elspeth/web/frontend/src/components/workspace/workspaceStatus.ts`) considered only
  `errors`, `is_valid` and `warnings`, and did not read `readiness.blockers` at all.
- The only actionable item — a validator suggestion and its Apply button — renders in
  `SuggestionList` inside
  `src/elspeth/web/frontend/src/components/sidebar/SideRailValidationBanner.tsx`, whose
  only non-test mount is `ChecksView`
  (`src/elspeth/web/frontend/src/components/workspace/ChecksView.tsx:43`), the
  Pipeline → Checks sub-tab.

## Impact

A user whose session is blocked sees a disabled button and a passive note, and has to
discover a sub-tab to find the one control that can unblock them. This is limited to
sessions where a readiness axis is false, but in those sessions the interface offers no
route forward.

## Fix

Done looks like this:

- A blocked session shows an actionable row above the chat input, without changing tabs.
  One consolidated decision panel, rendered whenever any readiness axis is false,
  following the existing inline-prompt pattern in the chat components (`role="region"`
  with a fixed `aria-label`). Its rows are a closed union of kinds — blocker, suggestion,
  or a pointer to an existing pending interpretation or proposal — comprising plain-words
  blocker lines naming which action is blocked (derived from the three readiness booleans,
  not from error codes); validator suggestions with the same Apply behaviour as today, a
  canned freeform prompt sent through `sendMessage`; a "Review again" canned prompt; and a
  link to Pipeline → Checks for the detail.
- The badge reads "Blocked" when `readiness.blockers` is non-empty, ordered between the
  error and warning arms of `projectValidationWorkspaceStatus`, so it cannot report a
  warning count while a blocker is live.

Verification is a component test at each of the two surfaces: given a validation result
with a non-empty `blockers` list, the panel renders with at least one actionable row and
the badge reads "Blocked"; given an empty one, neither appears.

Scope, size and known limits:

- Frontend only; `ChecksView` is untouched. Moderate size — two components and a status
  helper, no server change.
- Validation suggestions are transient: only the compose response and live validation
  carry them, and `GET /state` returns null, so after a page reload the panel shows the
  blocker without Apply rows. Backfilling them is a server wire change and a separate
  piece of work.
- The advisor blocker computes a `suggestion` value in the composer service that is then
  discarded rather than sent; surfacing it is likewise a separate wire change.
- Migrating interpretation approvals and proposal approve/reject into the same panel is
  deliberately out of scope here.
- Disclosure of the advisor's own text is not in scope.
