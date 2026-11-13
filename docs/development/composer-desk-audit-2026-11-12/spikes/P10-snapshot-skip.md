# P10 scope: skip identical state snapshots (CDA-23)

Scoped 2026-11-13 on `ccr-d14093ba-glu6bw` (HEAD `58e31760`). This was a
read-only investigation: nothing under src/, tests/, docs/ or evals/ was
edited. The evidence labels mean:

- **READ**: from reading the source at HEAD.
- **EXECUTED**: from running a command. The command is named in each case.

The lane brief comes first. It replaces the CDA-23 stub at
`work-packages.md` ~L841. The findings follow it.

---

### CDA-23: Skip identical state snapshots; surface only real changes (P10; ruling approved)

- **Goal.** A composer write whose result equals the session head adds no
  `composition_states` row and does not create a new version. "Equals the
  head" is defined by the no-op rule below. The caller gets the existing
  head record back, and an audit row records that nothing was written.
  - Users then see a new version only when the pipeline changed.
  - Today every mutating tool-loop turn ends on a duplicate `post_compose`
    row. The header shows that row as "vN (current), no visible change"
    (findings F1 and F3).
  - Any content that differs from the immediately previous row still adds a
    row. The sequence A→B→A adds three.

- **No-op rule.** This is one predicate in one new module,
  `src/elspeth/web/sessions/snapshot_noop.py`. It is evaluated **inside the
  writer's locked transaction, against the head row read in that same
  transaction**. A candidate is a no-op only when **all** of these hold:
  1. the session has a head row (the first save of an empty session always
     inserts);
  2. the authored-content hash is equal. This is `composer_authority_hash`
     over `sources`, `nodes`, `edges`, `outputs` and `metadata`, the same
     preimage as `composition_content_hash` (`composer/pipeline_proposal.py:746`)
     and `_composition_state_data_content_hash`
     (`sessions/proposal_authority.py:222`). The repository already treats
     those two as equal across a DB round-trip (`sessions/service.py:3628`
     vs `:3757`). Compare decoded values in Python, never with SQL JSON
     equality;
  3. the persisted verdict is equal:
     - `is_valid` is equal;
     - the multiset of `(error_code, component)` pairs is equal. `None`
       counts as `[]`, and message text is ignored because it is derived from
       the content;
  4. **turn-end writer only:**
     - the resolved completion-gate facts equal the head's effective gate
       facts. An absent fact counts as "no gate withheld", the equivalence
       `_validate_terminal_graph_review` already applies
       (`sessions/service.py` ~L7201-7208);
     - `repair_turns_used == 0`. A repair turn keeps its row, because the
       row is the counter's only durable home (F6).

  Nothing else takes part in the decision: not `composer_meta`'s
  `validation_lane`, `ingress`, `chat_ingress_inputs` or `implicit_decisions`,
  and not provenance, timestamps, lineage or creator. When any check fails,
  the writer behaves exactly as it does today.

- **Writers in scope.** Only these two composer authoring writers:
  - **Mid-turn writer** (`provenance="tool_call"`): `SessionServiceImpl.persist_compose_turn`
    (`sessions/service.py:1921`). The per-row insert is at ~L2142-2160, under
    the existing lock. The stale guard is at ~L2095-2113.
  - **Turn-end writer** (`provenance="post_compose"`): `_run_composer_turn`
    (`sessions/composer_turn.py:1166-1270`), which calls `save_composition_state`
    (`sessions/service.py:6385`), which calls
    `_RepositoryCompositionStateMutations.append_state`
    (`coordination/repository.py:858-955`).

- **Explicitly out of scope, unchanged.** Each of these writers binds a
  pre-allocated new id or has its own receipt or lineage contract:
  - revert (`revert_state_for_operation_receipt`, `service.py:8020`, which
    has the frontend label "Reverted to vN");
  - fork (`settle_fork_operation_receipt`, `:8685`, and
    `repository.py:4206 insert_child_state`);
  - interpretation resolve (`resolve_interpretation_event`, `:5141`);
  - the opt-out auto-resolution append (`repository.py:1359-1380`, through
    `pending_interpretation.py:2147`);
  - proposal settlement (`_settle_pipeline_composition_proposal`, `:3582`),
    whose `candidate_state_id` is bound into the receipts;
  - YAML import and the e2e seed (`save_composition_state_with_interpretations`,
    `:6450`), where interpretation events reference the pre-allocated
    `state_id`;
  - the failure-path partial persists (`routes/_helpers.py:3346-3813`:
    `plugin_crash_persist`, `convergence_persist` and `preflight_persist`,
    whose provenance is pinned in `test_routes.py` ~L11520).

  The corpus contains 0 duplicates from any of these (F6). Widening the rule
  to them would be a separate ruling.

- **What callers receive on a no-op.**
  - **Mid-turn.**
    - No row is inserted.
    - The tool chat row is written with `composition_state_id=NULL`, the
      same shape as a read-only tool.
    - `current_state_id` stays at the head, so the next call's
      `expected_current_state_id` still matches.
    - `inserted_state_ids` omits the call. If nothing was inserted, the
      result is `checkpoint_disposition="no_new_checkpoint"`, which already
      exists (`service.py:2261-2265`). The receipt count therefore counts
      real revisions only.
    - The private evidence audit row stays bound to the head, as today
      (`service.py:2181-2194`).
  - **Turn-end.**
    - Add a new service method with a typed result. Do not overload
      `save_composition_state`, so the other callers keep "always inserts"
      semantics:

      ```
      save_composition_state_unless_unchanged(...) -> CompositionStateSaveOutcome(record, created: bool)
      ```

    - On `created=False`, `record` is the **effective** head record
      (`_effective_state_record_on_connection`, `service.py:7146`).
    - `_run_composer_turn` then builds `state_response` from that record with
      the live strict validation. `post_compose_state_id` becomes the head
      id, so the assistant row binds to the head.
    - The graph-review fact path, today's `else` branch at ~L1273-1306, is
      reused with `state_id = head id`, not `compose_base_state_id`. Its
      guard at L1276-1277 must compare against the head's content.
    - The "saving" progress copy (L1245, "A new composition state version is
      being stored…") and the completion headline (L1358, "The composer has
      updated the pipeline.", keyed on the in-memory version) must key on
      whether the persisted head moved during the turn.

- **How the no-op is recorded (attributability).** Write one closed-shape
  audit chat row:
  - `role="audit"`, `writer_principal="compose_loop"`;
  - `tool_calls=[{"_kind": "composition_state_unchanged", …}]`, bound to
    the head state id.
  - Fields: `session_id`, `operation_id`, `operation_epoch`, `tool_call_id`
    (null for the turn-end case), `head_state_id`, `head_version`,
    `content_hash`, and `skipped_provenance`
    (`"tool_call"` | `"post_compose"`).
  - The turn-end case also records `validation_lane: "strict"` and
    `repair_turns_used`.

  This mirrors the `AdvisorGraphReviewFact` precedent: an audit row, never
  another state row (`protocol.py:1189`). The mid-turn case adds 1 to the
  `_reserve_sequence_range` count per skipped row (L2115), so decide the
  skips before reserving; they can be computed in memory. Add the new
  `_kind` to:
  - the fork copy filter (`service.py:8429-8436`). Drop it like the private
    evidence: it is bound to parent operations;
  - any closed audit-kind or wire-census lists the gates find.

- **What the user sees differently.**
  - **Header.** After a mutating turn, the current version is the last real
    edit, labelled "Applied: …", instead of a "no visible change" duplicate.
    Version numbers have fewer gaps.
  - **Tool card.** A no-op mutation shows no `vN` badge.
  - **Completion badge.** A turn that changed nothing reads "Response ready"
    instead of "Pipeline updated" (`sessionStore.ts:529`).
  - **Receipt.** "covers N saved draft revisions" counts real revisions.
  - **Open approvals.** An open approval is no longer superseded by a turn
    that changed nothing.
  - **Pending proposals.** A pending proposal whose base is the head is no
    longer made stale by such a turn (F2).
  - **Optional, recommended (23b).** Add a closed outcome
    `UNCHANGED = "unchanged"`:
    - in `_ToolCallOutcomeKind` (`routes/_helpers.py:383`), derived from the
      `composition_state_unchanged` row, never from tool names;
    - in the frontend union (`types/index.ts:110`);
    - as the label "No change" in `outcomeLabelParts`
      (`toolCallDescriptions.ts:168`).

    Without this, a no-op mutation renders "Completed:", the existing
    `COMPLETED` fallback at `_helpers.py:556`.

- **Files.**
  - `src/elspeth/web/sessions/snapshot_noop.py` (new): the predicate and the
    audit-envelope builder.
  - `src/elspeth/web/sessions/service.py`:
    - `persist_compose_turn` (~L2095-2265);
    - the new `save_composition_state_unless_unchanged` beside
      `save_composition_state` (~L6385). Use the
      `_session_process_locked_begin`, `_session_write_lock`,
      `_require_session_operation_context_on_connection` and
      `_insert_composition_checkpoint` pattern of
      `save_composition_state_with_interpretations` (~L6503-6545), so that
      `_effective_state_record_on_connection` is available in the
      transaction;
    - accept the `required_work` ticket as `save_composition_state` does
      (~L6414-6447);
    - the fork filter (~L8429).
  - `src/elspeth/web/sessions/protocol.py`: the new method and
    `CompositionStateSaveOutcome`.
  - `src/elspeth/web/sessions/composer_turn.py`: L1166-1306 (the branch
    condition, the saving copy, reuse of the review branch), L1358 (the
    headline).
  - `src/elspeth/web/composer/turn_audit.py` ~L583-603: the
    `expected_state` versus witness check when the *last* bumped call was
    skipped. Its content equals the last inserted row; verify that
    `authored_state_hash` agrees, or select the last *inserted* outcome.
  - `src/elspeth/web/sessions/routes/_helpers.py` ~L383-556 (23b only).
  - Frontend (23b only):
    - `src/elspeth/web/frontend/src/types/index.ts:110`;
    - `components/chat/toolCallDescriptions.ts:168-190`;
    - `components/chat/ToolCallCard.tsx:105-109`.
  - `evals/lib/composer_rgr_score.py:444-463`: an absent
    `composer_meta.repair_turns_used` reads as 0. The no-op rule keeps the
    row whenever the value is greater than 0, so absence now means the
    final turn made no repairs. Update the docstring at ~L93-97.
  - Whole-tree pin files you will hit (`CONTRIBUTING.md` § Whole-tree gates):
    - `tests/helpers/web_reserve_provider_mint_contract.py` pins the full
      source of `_run_composer_turn`, including the L1245 copy;
    - `tests/helpers/required_work_reserve_recipes/reviewed-full-source-guards.json`
      holds the sha256 of `service.py` and `protocol.py`;
    - `tests/unit/architecture/test_web_landscape_mutation_fencing.py:5821-5825`
      (`_reserve_CANCEL_CONSUMERS`) holds the sha256 of `composer_turn.py`;
    - `reviewed-writer-recipes.json` and `reviewed-non-dml-recipes.json`, if
      the writer recipe snapshot changes.

    Re-pin them only after the reviewed change, never to make a red go away.

- **Test files.**
  - **New:** `tests/unit/web/sessions/test_snapshot_noop.py` (SQLite, real
    service). Cover each of these:
    - two identical mid-turn payloads give 1 row; the second tool row has
      `composition_state_id IS NULL`; one `composition_state_unchanged` row
      is bound to the head;
    - A→B→A gives 3 rows, so equality with an older row does not count;
    - identical content with a different `is_valid`, a different error
      `(code, component)`, or different gates mints a row;
    - a post-compose save with `repair_turns_used > 0` mints a row;
    - the first save in an empty session mints a row;
    - an open approval survives a no-op save and is superseded by a real
      change;
    - a `PresentBase` proposal stays settleable after a no-op turn and goes
      stale after a real change;
    - `[]` and `None` error lists are treated as equal.
  - **New, in `tests/unit/web/sessions/test_run_composer_turn.py`.** A
    tool-loop turn whose final state equals its last mid-turn row:
    - writes no `post_compose` row;
    - returns `response.state.id` equal to the head;
    - binds the assistant row to the head;
    - completes with the "response ready" headline when the turn netted no
      head move.
  - **New, PG:** a case in `tests/testcontainer/web/test_session_operation_fence_postgres.py`
    (that file already saves `post_compose` at L94-120). On PostgreSQL an
    identical turn-end save reuses the head, and the hash of a JSON
    round-trip equals the in-memory hash. The `sources` order is bound by
    the hash preimage.
  - **23b:** `tests/unit/web/sessions/routes/test_tool_call_outcomes.py`
    gets an `unchanged` projection test plus a negative control (a read-only
    tool stays `completed`). In Vitest, extend `toolCallDescriptions.test.ts`
    and `ToolCallCard.test.tsx`.
  - **Pinned tests that must change** (EXECUTED: both pass today,
    `exit=0`). In `tests/unit/web/sessions/test_persist_compose_turn.py`,
    two tests depend on identical payloads minting versions:
    - `test_compose_checkpoint_returns_committed_exact_row_witness_and_distinct_no_new_state`
      (L131, the same `data` twice, asserts `witness.state.version == 2`);
    - `test_persist_compose_turn_rederives_predecessor_lineage` (L1481,
      `CompositionStateData()` three times, asserts versions `[1, 2]` and
      v3).

    Make their payloads distinct; they test the witness and lineage, not
    duplication. Other tests that reuse identical states may surface. Find
    them by running the suites below and fix them case by case. Do not
    assume there are none.
- **Test command.**

  ```bash
  pytest tests/unit/web/sessions/test_snapshot_noop.py \
    tests/unit/web/sessions/test_persist_compose_turn.py \
    tests/unit/web/sessions/test_run_composer_turn.py \
    tests/unit/web/sessions/routes/test_tool_call_outcomes.py \
    tests/unit/architecture/test_web_reserve_reviewed_contracts.py \
    tests/unit/architecture/test_web_landscape_mutation_fencing.py -n 4 -q
  ```

- **Also run.**
  - `pytest tests/unit/web -n 4 -q`.
  - **The testcontainer suite applies**, because the change touches the
    state insert path under the session lock: `pytest tests/ -m testcontainer -n 0`.
  - Before merge, the full-suite gate
    (`scripts/full-suite-gate.sh --execute --detach`). The change alters
    shared session-persistence behaviour, so its reach cannot be bounded by
    the targeted suites.
  - 23b, Vitest by hand:
    `npm --prefix src/elspeth/web/frontend run test -- toolCallDescriptions ToolCallCard HeaderVersionSelector versionLabels`.
- **Acceptance.**
  1. Every bullet in the new tests passes, and each new test fails at an
     assertion on base.
  2. Re-run the F6 instrument on one fresh S4-shaped replay
     (`p10_measure.py` below). It counts 0 `tool_call→post_compose`
     duplicates and 0 `tool_call→tool_call` duplicates, while the A→B→A
     rows remain.
  3. Every out-of-scope writer still mints on identical content. Pin one
     test for revert and one for interpretation resolve so the boundary is
     enforced.
  4. No composer invariant is touched: no provider call is added or removed
     on any transition, and there is no tutorial-specific branch.
  5. Run the keyless trust-tier scan before and after, and report the
     finding-set diff, not zero (AGENTS.md).
  6. `report.md` states:
     - the measured SQL-statement delta per mutating call (the predicate
       reads the head row once per transaction);
     - that the other half of P10, "prove the fence once per transaction",
       is **not** in this lane.
- **Depends on.** CDA-21, because of shared pin files:
  - `test_web_landscape_mutation_fencing.py` `_reserve_CANCEL_CONSUMERS`
    carries `composer_async_worker.py`;
  - `reviewed-full-source-guards.json` carries
    `composer_operation_authority.py`.

  CDA-23 shares no source file with any other lane. A grep of
  `work-packages.md` finds no other lane naming `composer_turn.py`,
  `sessions/service.py`, `protocol.py`, `turn_audit.py`, `routes/_helpers.py`,
  `ToolCallCard`, `toolCallDescriptions` or `types/index.ts`.
  - It does not need to touch `turn_effects.py` (CDA-07/08) or
    `test_checkpoint_receipts.py` (CDA-08). That test's empty-session save
    still inserts (L370-389).
  - **Placement:** wave 4 (also), after CDA-21 and parallel with
    CDA-14/15/16/17/22.
- **Size.**
  - **23a (backend):** about 1.5 agent-days. That covers both writers, the
    predicate, the audit row, the eval, re-pinning the gates, and the PG run.
  - **23b (SPA `unchanged` outcome):** about 0.5 agent-day.
  - One lane, or 23a then 23b. 23a alone delivers the header fix, F1.
- **Risks.**
  - **Effective versus stored gates on the head.** The predicate must read
    gates through `_effective_state_record_on_connection`, because a
    review-fact overlay can differ from the stored `composer_meta`.
    Mid-turn heads carry no `completion_gates` at all
    (`turn_audit.py:147-151`). If the comparison is wrong, the result fails
    conservative: it mints, as today, and never drops a withheld gate.
  - **The head's `validation_lane` stays `authoring_only`** after a skipped
    turn end. The strict lane's agreement is recorded only in the audit
    row. An auditor reading `composition_states` alone sees less than
    today. That is the intended trade, and the maintainer should confirm it
    (question 1).
  - **The fallback drain writer** (`_persist_tool_invocations`) still stamps
    `applied` from in-memory `version_before` and `version_after`
    (`_helpers.py:488-507`), not from rows. On a no-op it can claim
    "applied v{in-memory}". This is pre-existing and out of scope, but the
    lane should note it.
  - **The planner and tool results keep in-memory version numbers** that
    now drift further from persisted versions. Nothing found compares them,
    but treat that as unverified (READ, not exhaustive).

---

## Findings

### F1. State rows are written per mutation and again at turn end, and the turn-end row is always a duplicate (READ + EXECUTED)

**Writers (READ).**

- **Mid-turn writer.** `turn_audit.py:413-417` attaches a state payload to
  every tool row whose in-memory `post_version > pre_version`.
  `CompositionState.with_*` always bumps `version` (`state.py:8219-8306`),
  so an identical upsert still counts as a bump. `persist_compose_turn`
  inserts one row per payload (`service.py:2142-2160`) through
  `_insert_composition_checkpoint` → `_insert_composition_state`
  (`:1723-1866`).
- **Turn-end writer.** `composer_turn.py:1166` runs whenever the in-memory
  version changed, and saves `post_compose` (L1256-1262) through
  `append_state` (`repository.py:858-955`).
- **Other insert sites:**
  - `service.py:3886` (proposal settlement), `:5570` (interpretation
    resolve), `:8113` (revert), `:8832` (fork);
  - `repository.py:1379` (opt-out append), `:4222` (fork child);
  - `_helpers.py` partial persists via `save_composition_state`.

**Stored hash (READ).** `composition_states` has no hash column
(`models.py:521-581`). Every content hash is computed on demand.

- `composition_content_hash` (`pipeline_proposal.py:746-772`) hashes only
  `sources`, `nodes`, `edges`, `outputs` and `metadata`. It excludes
  `version`, `is_valid`, `validation_errors`, `composer_meta`, ids,
  timestamps, provenance and lineage. None of those volatile fields is in
  the preimage.
- `_composition_state_data_content_hash` (`proposal_authority.py:222-231`)
  uses the same preimage over `CompositionStateData`.
- `turn_effects.authored_state_hash` (`turn_effects.py:54-57`) is a separate
  hash over `to_dict()` minus `version`.

**Measured (EXECUTED).** The instrument is `p10_measure.py` (scratchpad,
reproduced below), run over all 11 `sessions.db` files under
`evals/composer-live-audit/runs/instances/`. It decodes the envelopes and
compares the five content columns with each session's immediately previous
row by version:

```
TOTAL rows 166  (tool_call 82, interpretation_resolve 67, post_compose 17)
eq_prev_content 20  = tool_call->post_compose 17 + tool_call->tool_call 3
eq_prev_full_incl_meta_valid 3
eq_older_not_prev (A-B-A) 1
```

- **All 17 of 17 `post_compose` rows equal their predecessor's content.**
- The 3 mid-turn duplicates came from `upsert_node`, `upsert_edge` and
  `patch_node_options` (S4 Luna v7→v8 and v23→v24; S4 Sonnet v17→v18). The
  instrument is `p10_tools.py`.
- 0 of 67 `interpretation_resolve` rows are duplicates.
- So 20 of 166 rows (12%) are no-ops. The skip rule would drop exactly
  those 20 and keep the A-B-A row.

**Instrument control (EXECUTED, `p10_control2.py`).**

- **Known-identical pair:** S4-luna `bdd357d4` (v4, `tool_call`) →
  `34102bbe` (v5, `post_compose`). All five content columns are equal;
  `validation_errors` and `composer_meta` differ.
- **Known-different pair:** S2-base `26e28839` (v1) → `24af10e1` (v2,
  `interpretation_resolve`). `sources` differs, and the instrument
  classifies the pair as "different".
- It matches the positive and rejects the negative.

**What the duplicate turn-end row actually carries (EXECUTED,
`p10_meta.py` and `p10_errs.py`).**

- In all 17, `composer_meta` differs in exactly these keys:
  `completion_gates`, `implicit_decisions`, `repair_turns_used` and
  `validation_lane`.
- `is_valid` is equal in 17 of 17.
- The errors differ only cosmetically:
  - `[]` versus `null`;
  - or the same `(interpretation_review_pending, drop_unit_price)` pair with
    a longer message.
- `completion_gates` is `{'schema_version': 2}` (no gate withheld) and
  `repair_turns_used` is 0 in the sampled rows.

**Conclusion.** The bare ruling ("content hash equals the previous row")
would drop real information in rarer cases:

- a strict-only verdict flip;
- a withheld advisor gate. Mid-turn rows carry no `completion_gates`
  (`turn_audit.py:147-151`), so the turn-end row is what carries a gate
  forward;
- a repair count.

That is why the rule above adds checks 3 and 4. Under those checks, all 20
corpus no-ops are still skipped.

### F2. Who depends on getting a NEW state id back, and what changes on a no-op (READ)

| Dependant | Binding | Effect of returning the head instead |
|---|---|---|
| Tool chat row / outcome (`service.py:2171-2180`; `_helpers.py:470-477`) | `composition_state_id` set ⇔ the call inserted a state, which makes it APPLIED with a `vN` badge | NULL → `COMPLETED` ("Completed:"). This is honest but vague; 23b adds `unchanged` |
| Checkpoint witness / receipt (`service.py:2236-2265`; `turn_audit.py:583-603`; `turn_effects.py:900-950`) | `inserted_state_ids` drives "covers N saved draft revisions" (`turn_effects.py:34-39`) | Counts real revisions; an all-no-op batch is `no_new_checkpoint`, which already exists. Verify the expected-state check when the last bumped call was skipped |
| Stale guard (`service.py:2095-2113`) / turn bound base (`composer_turn.py:900-911`) | head id equality | Unchanged head → still matches. No new conflicts |
| Approvals (`supersede_open_approvals`, run on every insert: `service.py:1863`, `repository.py:946`, `:1421`) | any new row supersedes all open approvals | **Behaviour change:** a no-op no longer supersedes. Content is identical, so the binding is still truthful. Pin it in a test |
| Proposals (`service.py:3865-3881`) | `PresentBase.state_id == head.id` **and** content hash | **Improvement:** a no-op turn no longer stales a pending proposal |
| Run-start permits (`run_start_permit_authority.py:66-74`) | `run.state_id` existence + approved bindings | Not affected |
| Dead-site interpretation sweep (`service.py:1858-1918`; `dead_site_supersession.py`) | runs on insert | Identical content extinguishes no sites → nothing to retire. Equivalent |
| Interpretation surfacing (`interpretation_surfacing.py:629-631`) | `current_state_id` | Anchors to the head, whose content is identical. Valid |
| Graph review fact (`service.py:7029-7110`, `:7146`, `:7158-7221`) | `fact.state_id == head`, content hash, gate fingerprint | Reused for the skipped turn end, bound to the head. The terminal validation already checks the *actual* head, not the turn start |
| Advisor binding | content-based (`completion_gate_fingerprint`) | Not affected |
| Graph recovery (`service.py:6697-6790`) | head id and parks | Not affected |
| Blobs (`blobs/service.py:246-288`) | active-run `state_id` | Not affected |
| Interpretations with pre-allocated `state_id` (`service.py:6450-6490`) | events FK to the *new* id | That is why this writer is **excluded** |
| Revert (`service.py:8020+`) | copies the target; header label "Reverted to vN" | Excluded. Every distinct content stays reachable, because only adjacent duplicates are skipped |
| Undo tool | none: there is no composer undo tool (grep of `composer/tools` for undo/revert) | — |
| Eval `_check_max_repair_turns` (`evals/lib/composer_rgr_score.py:444-463`) | `composer_meta.repair_turns_used` on the final state | Absent on a skipped turn → today that is AMBER. Change absence to 0; the rule keeps rows when the count is above 0 |

### F3. Where versions surface to users (READ)

- **Header history.**
  - `HeaderVersionSelector.tsx:89-98` already hides every snapshot-only row
    (`versionLabels.ts:227 isSnapshotOnly`, which compares content) except
    the **current** one.
  - The current one is rendered "no visible change" (L377-389).
  - Because every mutating turn ends on a duplicate `post_compose`, the
    current version after each such turn shows "no visible change" while
    the real edit sits one row below it. This is the "sloppy" surfacing.
  - After the change, the current row is the edit itself.
- **Tool card.** `ToolCallCard.tsx:105-128` shows a `v{applied_state_version}`
  badge, "Pipeline advanced to version N".
- **Completion badge.** `sessionStore.ts:529` computes
  `changed = nextState.version !== beforeVersion`, which feeds
  `completionOutcome.ts:18-22` ("Pipeline updated" versus "Response ready").
  Today a no-op turn still reads "Pipeline updated", because `post_compose`
  moved the version.
- **Backend copy.** `composer_turn.py:1358` says "The composer has updated
  the pipeline." whenever the in-memory version moved. L1245 says "A new
  composition state version is being stored". Both are false on a no-op.
  The only test that pins them is the source contract
  (`tests/helpers/web_reserve_provider_mint_contract.py`).
- **Receipt.** `turn_effects.py:34-39` reads "covers {count} saved draft
  revisions…".

### F4. Audit semantics (READ)

- **ADR search.** A search of `docs/architecture/adr` finds no ADR that
  defines `composition_states` version semantics. Only incidental
  mentions exist (ADR-025, 040, 042).
- **Session store, not Landscape.** The operator ruling of 2026-09-02
  (`models.py:1296-1301`, `turn_audit.py:168`) places composer authoring
  history in the **session store**, not Landscape. `composition_states`
  is the session's audit-attribution surface and runs reference it by FK.
  The provenance enum is closed (`models.py:565-575`).
- **What is already recorded for a no-op tool call** (`service.py:2117-2215`):
  - the assistant row with the redacted `tool_calls`;
  - the tool row (redacted result and visible-event companion);
  - a private evidence audit row with canonical arguments and the result
    hash, already bound to the head (`composition_state_id=current_state_id`).
  - The call, its arguments, its result and the state it ran against are
    therefore attributable.
- **What is missing** is an explicit "this mutation persisted nothing"
  fact. Without it, a successful mutation that wrote nothing looks like a
  read-only lookup. The classifier must not infer this from tool names
  (`_helpers.py:446-449`).
- **Recommendation.** Record it with the `composition_state_unchanged`
  audit row in the brief. The precedent is `CompositionStateReviewProjection`:
  "Explicit live review overlay; never another composition-state row"
  (`protocol.py:1189`).

### F5. Edge cases (READ)

- **Interpretation-only changes.** These never write `composition_states`;
  they write `interpretation_events`. The resolve and opt-out appends do
  change content (0 of 67 corpus duplicates) and are excluded anyway.
- **A→B→A.** The rule compares with the immediately previous (head) row
  only. The corpus has 1 such row (S4 Sonnet), and it must still mint.
- **Concurrency.**
  - The decision must be made under the same `_session_write_lock`
    transaction as the insert. `persist_compose_turn` already reads the head
    under the lock (L2095-2101). `append_state` reads `MAX(version)` inside
    the authority transaction.
  - The COMPOSE fence prevents a second writer from appending mid-turn.
  - The stale guard stays as it is.
- **PostgreSQL versus SQLite.**
  - Compare decoded values in Python. PostgreSQL `json` has no equality
    operator, and `jsonb` would reorder keys while the hash binds `sources`
    order (`pipeline_proposal.py:749-753`).
  - Because the change touches the locked insert path, the testcontainer
    suite applies. An existing PG test already exercises
    `save_composition_state(post_compose)`
    (`tests/testcontainer/web/test_session_operation_fence_postgres.py:94-120`).

### F6. Turn-end bookkeeping that has no other durable home (READ + EXECUTED)

- **`repair_turns_used`.** It is written only into `composer_meta`. A grep
  of `src/elspeth/web` finds it in `composer/protocol.py` and in the
  `_compose_loop_carriers`/`service.py` plumbing; it is durable only in
  `composer_meta`. It is read by the convergence eval.
- **`implicit_decisions`.** A pure projection of content
  (`implicit_decisions.py:108`; `routes/sessions.py:508-540`).
- **`validation_lane`.** A marker (`models.py:541-556`).
- **`completion_gates`.** These carry forward only through turn-end rows;
  mid-turn payloads omit them (`turn_audit.py:147-151`).
- **Frontend.** No component reads any of these keys (grep of
  `frontend/src`, test files excluded).

---

## Questions for the maintainer

1. **No-op rule.** The bare ruling ("content hash equals the previous row")
   would also drop:
   - a strict-only verdict change;
   - a withheld advisor gate;
   - a repair count.

   The brief therefore adds verdict, gate and `repair_turns_used == 0`
   checks. All 20 corpus no-ops are still skipped. On a skipped turn end
   the head row keeps `validation_lane: authoring_only`, and the strict
   agreement is recorded in the new audit row. OK?
2. **Writer scope.** The brief covers only the mid-turn `tool_call` and
   turn-end `post_compose` writers. It leaves revert, fork, interpretation
   resolve and opt-out, proposal settlement, YAML import and the failure
   partial persists untouched. OK, or widen?
3. **Optional parts.** Should 23b (an `unchanged` tool-call outcome, "No
   change", in the SPA) ship with it? Is reading an absent
   `repair_turns_used` as 0 in the eval scorer acceptable?

## Instruments (gitignored lane directory)

All five scripts are in
`evals/composer-live-audit/spikes/P10-snapshot-skip/`:

- `p10_measure.py`: duplicate counts;
- `p10_control2.py`: the positive and negative controls;
- `p10_meta.py`: composer_meta and verdict differences;
- `p10_errs.py`: error samples;
- `p10_tools.py`: the tools behind the mid-turn duplicates.

Each was run as `python3 -I <script> $(find evals/composer-live-audit/runs -name sessions.db | sort)`
(`p10_control2.py` takes explicit db/id pairs).
