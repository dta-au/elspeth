# D1 expected proposal resource race (T4)

Worktree composer-remediation-wave1; parent supplies current base after state/blob admission. Read AGENTS/CONTRIBUTING. Requirements: narrow resource race refusal in both freeform and planner publication, preserving corruption/cancellation/required-work authority. Read d1-canonical-map.md section F for exact seams and controls; planT4. No agents/commits/broad suites.

Add named expected domain exception raised only by validate_proposal_blob_references queried missing/foreign/known non-ready resource outcomes. Keep malformed input/owned authority parsing distinct; corrupted owned status must not be laundered into normal refusal. Do not blanket catch ValueError around validator/caller. Preserve authoritative transaction checks before proposal/event insert; no unlocked precheck substitution. Safe fixed public copy must not echo foreign resource or arbitrary parser/SQL details.

Freeform tool_batch: catch this exact expected creation failure, cache canonical refused/unapplied result through preceding admission disposition, feedback/audit/outcome once, no proposal or graph row, no duplicate dispatch. Other failures/cancellation remain faulted with existing custody. Success count/cap only advances after actual row publication. Tests use real source after admitted preview then changed blob resource before create.

Planner planning_application._stage_pipeline_plan: preserve already-persisted cohort exactly once; return fixed backend unchanged ComposerResult, no commit intent/no newly claimed runtime verdict, no fabricated provider decline or successful dispatch. Non-required create wrapper catches only exact domain exception; concurrent cancellation retains its original exception/cause. Required path must project_creation_handoff first and may return expected refusal only for rowNone + exactly one domain error + no creation cancellation. Preserve failure receipt and complete actual ticket obligations, no fake SQL-returned result or handoff shape. Returned row plus post-SQL telemetry fault is NOT no-proposal refusal; unknown/grouped faults remain existing failure path.

Pin validator positive/negative classification, real freeform and both planner publishers/trust modes, resource change before creation, exact no-write/event/audit cardinality, later valid repair, corruption/ValueError/SQL negative controls, cancellation during failed creation, returned-row-plus-telemetry failure. Use existing real session/required-work fixtures in map. Red controls before fix; focused tests then scoped type/lint/contracts/gates relevant. Parent final full/static/PostgreSQL. Both PYTHONPATH roots and explicit cwd/provenance, logs/exits in lane.

Freeze/report d1-stage-race-report.md and d1-stage-race-review.diff including new files, exact changed behavior and verification limits.

## Source preflight clarifications (ff755b12b, 2026-11-09)

- Section F's “non-ready” means only a known non-ready member of the existing
  `contracts.blobs.BLOB_STATUSES` authority (`pending` or `error` today). For
  a same-session row, validate the owned status as an exact string in that
  authority before classifying non-readiness; malformed/unknown persisted
  status is an integrity fault, not the new domain exception. Reuse the
  existing status authority rather than adding a second status vocabulary.
- Freeform has TWO creation calls: `tool_batch.py:2208` calls canonical
  `create_pipeline_composition_proposal`, and `:2235` calls
  `create_composition_proposal`. Required-control finalization can promote
  an ordinary incremental tool into `proposal_tool_name="set_pipeline"`
  (`:2138`). Catch the expected resource failure at both actual creation
  branches, including that promoted route. Cache the refused result and
  bypass the entire proposal-success block (`:2258` onward), then fall
  through to the existing canonical dispatch/outcome path; do not call the
  handler again or add a second audit/feedback path. The ordinary completed
  refusal dispatch uses existing audit semantics; it must not claim a
  successful business mutation or proposal publication.
- For cancellation of FAILED creation, the existing test fixture's pause
  at `INSERT INTO composition_proposals` cannot be reused unchanged:
  reference validation fails before that statement. Change the resource
  after valid admission but before creation, and synchronize cancellation
  at the actual validator/SQL callable before its expected failure. Retain
  real SQL, privately issued handoffs, original exception identity, and
  completed SQL/projection tickets. Keep the INSERT barrier for the
  separate returned-row/post-SQL-telemetry control.
