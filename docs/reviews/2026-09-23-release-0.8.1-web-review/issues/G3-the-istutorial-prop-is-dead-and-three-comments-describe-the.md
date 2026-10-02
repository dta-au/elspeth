# G3. The `isTutorial` prop is dead, and three comments describe the removed column editor

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Guided lane (retiring) |
| Review line | frontend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | fe-05#3 |

## Finding

- **Location:** `frontend/src/components/chat/guided/InspectAndConfirmTurn.tsx:10`, `GuidedTurn.tsx:115`, `ProposePipelineTurn.tsx:368`, `MultiSelectWithCustomTurn.tsx:61,172`
- **Finding and suggested disposition:** This is a half-finished removal from 07faf477e. The dead prop also suggests tutorial-only behaviour, which the invariants forbid. Delete it along with the comments.
- **Sources:** fe-05#3.

## Source findings and verification

### fe-05#3: isTutorial prop is dead after the column editor was removed; three comments still describe the removed editor

- **Reported at:** `src/elspeth/web/frontend/src/components/chat/guided/InspectAndConfirmTurn.tsx:10`; reviewer severity low; category dead-code; diff-anchored True.
- **Summary:** 07faf477e removed the rename/remove column editor and its firstRunRef focus management. `isTutorial?: boolean` is still declared, and GuidedTurn.tsx:115 still passes it, but the component never reads it. ProposePipelineTurn.tsx:368 still says the widget 'hides Edit columns…' in tutorial mode, and MultiSelectWithCustomTurn.tsx:61 and :172 cite InspectAndConfirmTurn's firstRunRef pattern, which no longer exists.
- **Failure scenario:** No runtime failure. A maintainer who follows the comments finds code that no longer exists, and the dead prop suggests tutorial-specific behaviour, which the no-tutorial-special-paths invariant forbids.
- **Evidence:** InspectAndConfirmTurn.tsx:6-19: the interface declares isTutorial and the destructuring omits it. GuidedTurn.tsx:110-116 passes isTutorial={isTutorial}. ProposePipelineTurn.tsx:368. MultiSelectWithCustomTurn.tsx:61, 172.
- **Suggested fix:** Remove the prop from the interface and from the GuidedTurn call site, and delete or rewrite the three comments. Do not invest further in the guided lane.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute this finding. At 74c0ce0db, InspectAndConfirmTurn.tsx still declares `isTutorial?: boolean` at line 10, but its destructuring at lines 15-19 takes only payload, onSubmit and disabled, and nothing in the file reads the prop. GuidedTurn.tsx:115 still passes `isTutorial={isTutorial}`. The diff for 7c986dc97..74c0ce0db shows that 07faf477e deleted both uses of the prop: the default `isTutorial = false` and the `{!isTutorial && (... Edit columns...)}` gate. It also deleted the firstRunRef focus effect, so leaving the interface field behind was a half-finished removal made inside the window. Three comments now describe code that no longer exists. ProposePipelineTurn.tsx:368 says InspectAndConfirmTurn "hides 'Edit columns…'", but that button is gone. MultiSelectWithCustomTurn.tsx:61 and :172 point readers to InspectAndConfirmTurn's "ref + effect + firstRunRef pattern (Task 7.3)", and `grep firstRunRef InspectAndConfirmTurn.tsx` finds nothing. The window did not edit those two comment files for this change, but it was the window's removal that made them wrong. The defect is real but has no runtime effect, and it sits in the guided lane, which is being retired, so severity stays low.
