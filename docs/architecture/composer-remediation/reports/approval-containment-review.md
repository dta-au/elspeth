# Review: explicit-approval containment (39da736ec, 320ff1cb1; base 2f38b807b)

Reviewer: independent read of the diff plus callers. Worktree
`.claude/worktrees/composer-0812-approval`. "Run" = executed; "Read" = by reading.

Run: `test_explicit_approval_containment.py`, `test_freeform_proposal_prevalidation.py`,
`test_compose_loop_persistence.py`, `test_proposal_cap_turn_counts.py`,
`test_tool_batch_rejection_llm_call_audit.py`, `test_error_code_redaction.py`
(`-n 4`): exit 0, 357 passed (log `.claude/lanes/approval-review/touched.log`).
No mutation run (read-only brief); the author's negative control was not repeated.
Added lines contain no `noqa`, `type: ignore`, `getattr`/`hasattr`, broad `except`, skip or xfail (grep, run).

## MUST-FIX

1. **The skill still tells the model to do what is now refused whole.**
   `src/elspeth/web/composer/skills/pipeline_composer.md:160,175-183,207`:
   "Choose the narrowest supported mutation", "bounded construction batches",
   "The narrower tools also support staged construction", and "use a full
   replacement only when you can supply the exact current bindings". The refusal
   (`tool_batch.py` ~L447) says "a single set_pipeline call containing every edit".
   Failure: in an `explicit_approve` session the model follows the skill, sends
   2+ narrow edits, all are refused (`approval_batch_not_atomic`), and it must
   re-author the whole pipeline (every existing binding) as one `set_pipeline`.
   That costs a provider round per multi-edit turn and invites drift when the
   pipeline is large. The author drafted the sentence but did not apply it.
   Apply it (with the `test_prompts.py` pin) or accept the refused round knowingly
   and say so in the changelog. Read.

## SHOULD-FIX

2. **Empty-pipeline planner path can still stage a second pending proposal.**
   `composer/service.py:1233-1241` routes every request while the saved state is
   structurally empty to `_plan_and_stage_empty_pipeline`; `planning_application.py:608`
   `_stage_pipeline_plan` never reads pending rows (grep: the only
   `list_composition_proposals` uses in `composer/` are `tool_batch.py:404,457`).
   A pending proposal does not change the saved state, so the common "first
   proposal pending, user types another message instead of deciding" flow stays
   on the planner and publishes proposal #2 against the same null base. The accept
   check (`sessions/routes/composer/proposals.py:439`) only fires for a non-null
   base, so accepting #2 after #1 silently overwrites rather than 409s. Both are
   whole pipelines, so no half-applied graph, but "one outstanding proposal" is
   false on the most common entry. The report (decision 4) discloses this; it is
   the first-message path, so a one-line pending check there is worth doing today
   (a refusal, not authoring). Read; not exercised.

3. **Refusal text can send the user to a dead button.** `tool_batch.py` "pending"
   message says "accept or reject". A stale pre-containment sibling can only be
   rejected (accept returns 409 `proposal_base_state_changed`). With decision 2
   (any pending composition proposal blocks) the model will say "accept or reject"
   about a proposal that cannot be accepted. Suggest wording "decide on it (a
   stale one can only be rejected)" or tell the model to name the proposal. Read.

4. **Over-count by name.** `_approval_containment_refusal` counts by tool name
   before the loop, so one valid mutation plus one mutation that then fails JSON,
   canonicalisation or required-path checks refuses the valid one too, with
   "carried 2". Safe (nothing applied) but the model is told both were refused
   for atomicity when one was malformed. Acceptable; note in the changelog. Read.

## NOTES (checked, no defect)

- **Composition-changing set is derived, not hand-listed.**
  `_stages_composition_proposal` = `is_mutation_tool and not is_blob_store_only_mutation_tool`,
  both from registry declarations (`tools/_registry.py`, `declarations.py:195-199`
  effects: `blob_store_only` is the only BLOB_MUTATION kind with non-GRAPH effects).
  It matches the staging predicate `explicit_approval_required` (`tool_batch.py:1819`)
  exactly, and covers `wire_secret_ref`, `set_source_from_blob(s)`, `wire_blob_inline_ref`.
  Read.
- **Races.** The pending read (`tool_batch.py:455`) uses a plain `engine.connect()`
  outside the publish transaction, so it is check-then-act. It is safe in the
  compose loop because compose is exclusive per session: DB unique index
  `uq_composer_async_operations_one_nonterminal_per_session` (`sessions/models.py:3694`),
  worker lock plus `SessionOperationConflictError` (`composer_async_worker.py:464-520`),
  and accept/reject hold the same compose lock. A batch stages at most one
  composition proposal, and later provider rounds re-read, so no in-loop path
  gets two. Concurrent accept/reject only shrinks the pending set. Not
  transactional, but it does not matter while the exclusion holds. Read.
- **Other publishers.** Only `tool_batch.py:2369` and the planner publish
  proposals. The MCP composer does not reach `run_tool_batch` (grep), so no bypass.
  `auto_commit` is gated by the same `explicit_approve` condition; the containment
  expression is `None` otherwise, and the unchanged `elif is_mutation_tool(...)`
  branch handles dependency refusals as before (diff read; auto_commit test run).
- **Audited path.** Refusal reuses the `batch_dependency_refused` block: one
  `finish_success` with a `success=False` result, one tool message, one persisted
  row per call, after audit-envelope open, nothing previewed or published (the
  inline-custody test 320ff1cb1 covers blob leakage). Nothing silently dropped. Run.
- **No stuck user.** `actionableProposals.ts` shows every `pending` proposal
  including stale ones, and reject has no base check. Backend filter excludes
  blob-only rows that the UI also shows, so the block is always visible and
  rejectable. Frontend rendering not exercised.
- **Invariants.** The server only refuses; no structure authored, no tutorial
  branch. Wording change keeps the five-key payload.
- **Test contract changes (9).** Reviewed the diffs of all test files.
  Cap tests moved from `set_metadata` to `delete_blob` with unchanged
  assertions: legitimate, composition prefix-publication is genuinely unreachable
  and blob proposals still reach the cap (cap tests ran green). The
  noop/refusal/argument 11-call composition variants are replaced by blob
  variants plus `at-cap-refusal`; coverage of "invalid composition calls do not
  count toward the cap" persists only via the single tail call. The inline-blob
  authority test now rejects the first proposal first; assertions unchanged.
  Weakest: `test_publication_resource_race_refuses_once_then_allows_valid_repair`
  lost canonical/promoted x same-batch and the "refusal plus ten admitted share
  one counter" boundary. A composition publisher plus a same-response composition
  repair is genuinely refused now, so that is unreachable, but the shared-counter
  property is now only indirectly pinned (blob variants). Not a weakened check,
  a real coverage reduction; accept.
- Not run: full suite, testcontainer, frontend.

## Verdict

**Land with named fixes:** apply or explicitly defer item 1 (skill text) and fix
the "accept or reject" wording (3); add the pending check to the empty-pipeline
planner (2) or record it in the 0.8.12 changelog as a known gap. The guard itself
is sound and auto_commit is unaffected.
