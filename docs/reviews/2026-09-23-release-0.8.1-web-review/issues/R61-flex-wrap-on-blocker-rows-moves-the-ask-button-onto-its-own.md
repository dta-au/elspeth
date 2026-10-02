# R61. `flex-wrap` on blocker rows moves the Ask button onto its own line, and the comment says layout is unchanged

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Frontend workspace, inspector and stores |
| Review line | frontend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | fe-05#1, fe-06#1 |

## Finding

- **Location:** `src/elspeth/web/frontend/src/components/chat/chat.css:2325-2336`, together with `:2301-2323`.
- **Wrong:** In a wrapping flex container, line breaks use the text span's max-content width, and `min-width: 0` does not reduce it. Ask therefore drops to a left-aligned second line whenever the detail is wider than one line, which is the usual case in the chat dock. The comment says "a row without a note … nothing wraps".
- **Fix:** Scope the wrap to rows that have a note (with a modifier or `:has()`), or set the text to `flex: 1 1 0`. Correct the comment.
- **Sources:** fe-05#1, fe-06#1.


## Source findings and verification

### fe-05#1: flex-wrap on blocker rows moves the Ask button to its own line; the comment says layout is unchanged

- **Reported at:** `src/elspeth/web/frontend/src/components/chat/chat.css:2329`; reviewer severity low; category ux-regression; diff-anchored True.
- **Summary:** b5e458d1d added `.decision-panel-item--blocker { flex-wrap: wrap; }` so the reviewer note (flex-basis 100%) gets its own line. In a multi-line flex container, items are placed on lines by their hypothetical main size. For the `flex: 1 1 auto` text span that is its max-content width, so the text no longer shrinks beside the button. The comment claims rows without a note are untouched, and that is false.
- **Failure scenario:** Blocker with an advisor note: the DOM order is text, then note, then Ask button. The 100%-basis note forces line breaks before and after it, so even with short detail text 'Ask the composer about this' lands on a third line at the left edge instead of beside the text. Blocker without a note but with a detail sentence wider than the panel (about 400-600px chat column): the text fills line 1 and Ask drops to line 2, left-aligned. Before the window, the text wrapped inside its span and Ask stayed on the right. The button is still reachable, so nothing is blocked, but the layout changed for every such row.
- **Evidence:** chat.css:2301-2306 `.decision-panel-item { display:flex; justify-content: space-between }`; chat.css:2313-2318 text `flex: 1 1 auto; min-width: 0`; chat.css:2325-2336 new wrap rule plus note `flex: 1 1 100%`, with the comment 'a row without a note still has only its two children, so nothing wraps'. DecisionPanel.tsx:228-252 renders span, then optional note div, then Ask Button, in that order.
- **Suggested fix:** Apply the wrap only when the row has a note, using a `decision-panel-item--has-note` modifier set in DecisionPanel when `row.note !== null`. Or render the note as a block after a flex wrapper around text + Ask. Fix or remove the comment.
- **Verifier (trace):** upheld, confidence high, severity low. The finding holds; I could not refute it. The `flex-wrap: wrap` rule sits on every blocker row, whether or not the row has a note. Nothing overrides the flex sizing of the text span or the Ask button. The Ask button renders in freeform mode, which is now the only mode. In a flex container that wraps, CSS Flexbox §9.3 step 5 assigns items to lines by their hypothetical main size. For a span with `flex: 1 1 auto` and `width: auto`, that size is the text's max-content width, and `min-width: 0` does not reduce it. So when the full detail text plus the gap plus the button is wider than the row, the button drops to its own line at the left instead of the text wrapping beside it. Before b5e458d1d the row did not wrap: the span shrank, its text wrapped inside it, and Ask stayed on the right. The new comment says a row without a note has only two children, so nothing wraps. That is false: two children can still wrap. In rows that have a note, the 100% basis also puts Ask on a third line. The button stays visible and clickable, so this is only a layout regression plus a comment that misdescribes the behaviour. Severity stays low.

### fe-06#1: Blocker-row flex-wrap moves the Ask button under the text even when there is no reviewer note

- **Reported at:** `src/elspeth/web/frontend/src/components/chat/chat.css:2329`; reviewer severity low; category ux-layout; diff-anchored True.
- **Summary:** `.decision-panel-item--blocker { flex-wrap: wrap }` combined with `.decision-panel-item-text { flex: 1 1 auto }` means line breaking uses the text's max-content width. On any blocker row whose detail doesn't fit on one line beside the non-shrinking Ask button, the button wraps onto its own line. The new comment says 'a row without a note ... nothing wraps and every other row's layout is untouched', which is false.
- **Failure scenario:** Narrow chat dock, blocker detail 'Completion advisory review did not clear after the available attempts.' plus a suggestion: the text's max-content width + gap + 'Ask the composer about this' is wider than the panel. Before, the text wrapped beside a right-aligned button. Now the button drops to a second line, left-aligned. With a note the order is text / note / button, so the button ends up below the note.
- **Evidence:** chat.css:2313-2318 (.decision-panel-item-text flex: 1 1 auto; min-width: 0), 2320-2323 (ask button flex-shrink: 0), 2325-2331 (new wrap + comment). DecisionPanel.tsx:226-251 renders span, optional note div, then Button. Per CSS Flexbox §9.3, items are collected into lines by outer hypothetical main size, which is the flex base size, i.e. max-content for flex-basis auto. min-width: 0 does not reduce it. Added in b5e458d1d.
- **Suggested fix:** Add `.decision-panel-item--blocker .decision-panel-item-text { flex: 1 1 0; }` so the text wraps internally beside the button, or scope the wrap to `.decision-panel-item--blocker:has(.decision-panel-reviewer-note)`. Correct the comment.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute this finding. The code trace confirms it. Commit b5e458d1d added `flex-wrap: wrap` to every `.decision-panel-item--blocker`, whether or not the row has a note (chat.css:2329-2331). The text span has `flex: 1 1 auto` and `min-width: 0` (chat.css:2313-2318), so its flex base size is its max-content width. `min-width: 0` sets a lower clamp only, so it cannot shrink the size used to break items into lines. The Ask button has `flex-shrink: 0` (chat.css:2320-2323). When the detail and suggestion text is wider than one line beside the button, the flexbox line-breaking step (§9.3) puts the button on a second line. There, `justify-content: space-between` with a single item places it at the start, so it sits left-aligned (chat.css:2301-2307). The text then shrinks to fill the first line on its own. Before this commit the row did not wrap: the text wrapped internally and the button stayed on the right. Nothing prevents this layout. No media or container query overrides `.decision-panel-item`, and no other stylesheet touches it; the only hits are in chat.css. Blocker details are long sentences, e.g. no_tool_policy.py:141-143, which is about 190 characters. So in the chat dock the wrap is the usual case, not an edge case. The new comment says a row without a note has only two children, so nothing wraps. That is false, because wrapping depends on content width, not on the number of children. The finding misreads nothing. The effect is only visual: the button stays usable and nothing becomes inaccessible. So it stays at low severity.
