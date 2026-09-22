# G2. The inspection confirmation must equal the observed headers, which strands CSVs with blank headers

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Guided lane (retiring) |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-04#2 |

## Finding

- **Location:** `web/composer/guided/stage_transitions.py:1058-1069`
- **Finding and suggested disposition:** "Looks right" sends the observed columns, which fails `non-empty`. Any other list fails `must match`. The rename editor was removed in 07faf477e. The duplicate-header half of the finding is wrong: that turn is never emitted, and this predates the window. Retirement resolves it.
- **Sources:** be-04#2.

## Source findings and verification

### be-04#2: Inspection confirmation must equal observed headers, stranding CSVs with blank or duplicate headers

- **Reported at:** `src/elspeth/web/composer/guided/stage_transitions.py:1068`; reviewer severity low; category ux-regression; diff-anchored True.
- **Summary:** transition_source_inspection_review now requires the submitted columns to equal facts.observed_headers, but the earlier checks at :1060/:1062 reject blank and duplicate columns. The CSV inspector keeps blank and duplicate header cells (it only warns), and the frontend's rename editor was removed, so no submission can pass.
- **Failure scenario:** In guided mode a user uploads a CSV whose header row is 'id,,name' or 'a,a,b', picks the csv plugin and submits options. The inspect_and_confirm turn appears. 'Looks right' sends the observed columns and fails with 'inspection columns must be non-empty' or 'must be unique'. Any other list fails with 'must match the observed headers'. The route accepts only edited_values and offers no back action, so the source step cannot be completed from this turn. Before the window, the 'Edit columns' editor let the user rename past it.
- **Evidence:** stage_transitions.py:1058-1069. source_inspection.py _inspect_csv: headers = tuple(h.strip() for h in rows[0]) with warnings only for empty or duplicate headers. At HEAD, InspectAndConfirmTurn.tsx handleLooksRight sends payload.observed.columns verbatim, and the editor was removed in 07faf477e (compare with git show 7c986dc97:.../InspectAndConfirmTurn.tsx). sessions/routes/composer/guided.py:2798-2810 accepts only edited_values.
- **Suggested fix:** Retiring guided mode resolves this; do not invest in the guided lane for it. Only if guided ships in a release before it is retired: reject blank or duplicate observed headers in transition_source_schema_form (:991), so the user never enters an inspection_review turn they cannot complete.
- **Verifier (trace):** upheld, confidence high, severity low. The blank-header half of the finding holds and I confirmed it at 74c0ce0db. The duplicate-header half is wrong. With duplicate headers the inspect_and_confirm turn is never emitted: the payload validator requires unique columns and rejects the turn when it is built. That failure predates the window and was not introduced by it. For a header row such as 'id,,name', the pinned code leaves no way to complete the turn. Before the window the removed rename editor allowed the user to replace the blank name and pass. The change is a deliberate ruling that inspection confirms the actual headers (docs/reviews/2026-09-22-chat-card-repairs.md:14-17). That ruling does not address blank headers, and the resulting dead end is unintended. Severity stays low because guided mode is being retired, the Guided radio is disabled, and only saved Guided defaults and the tutorial still reach this path.
