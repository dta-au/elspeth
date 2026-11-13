# Peer review of STITCHED-PLAN.md (2026-11-13)

Reviewed: `docs/architecture/composer-remediation/STITCHED-PLAN.md` on
`integration/composer-remediation` at `8f5ca4292`.

Method: read-only. Code claims were checked by reading the worktree or with
single `grep` / `git` commands. No test was run. Each finding is marked
**[code]** (read in the worktree) or **[inferred]**.

**Verdict: CLEARED WITH FIXES.** Five blocking fixes (B1 to B5), all edits to
the plan. Wave 1 may start once they are applied. CDA-05 and CDA-06 are
unaffected by B1, B2 and B4 and need only B3.

---

## Blocking findings

### B1. CDA-02: the S-2 default does not do what the plan says, and the lane's scope misses three baseline producers

**(a) Three more seams pass a diff baseline. [code]** The wave-1 report and the
plan name only the two `tool_batch.py` seams. `ToolContext.baseline` is also
set at:

- `web/composer/pipeline_planner.py:3822` (`baseline=current_state`, the
  planner route's discovery dispatch; `diff_pipeline` is the first entry of
  `PLANNER_DISCOVERY_TOOL_NAMES`, `capability_skill.py:17-18`);
- `web/composer/pipeline_commit.py:517` and `:593`;
- `composer_mcp/server.py:412` and `:878`, where the baseline is the state at
  session create or load (`:732`, `:918-923`).

`DiffResponse` (`tools/_generation_responses.py:89-105`) is a closed model and
`diff_states` is shared. A `baseline_kind` key therefore has to be produced, or
be legally absent, on all of these. The MCP baseline is neither
`request_start` nor `last_accepted`, so a lane told "do not invent a third
baseline" has no legal value to emit there.

**(b) The default leaves P-B2 unfixed in two of three cases. [code for the
explicit-approval case, inferred for auto-commit]**

| Session | Baseline under the S-2 default | "What changed?" one turn after an edit |
|---|---|---|
| Explicit approval, edit accepted | Newest committed proposal is that edit. `accept_pending_ordinary_proposal` writes a new checkpoint and sets `committed_state_id` to it (`sessions/mutation_capabilities.py:466-513`), so the baseline is the current head. | Zero changes. The P-B2 symptom remains. |
| Auto-commit, no committed proposal | Request start. | Zero changes. The P-B2 symptom remains. |
| Accepted pipeline proposal, then auto-commit edits | The pipeline acceptance (`sessions/service.py:3966-3995`). | Every edit since that acceptance, growing for the life of the session. This is the CDA "s01 T6" shape, and the only case the default fixes. |

So §2's "P-B2 (CDA-02)" under "Open, covered by a CDA lane" overstates. The
default also removes the shipped "this request's edits" view (`a2ad64974`) for
every session that holds a committed proposal. That is a model-facing change
on a definition the maintainer deferred ("settle 'accepted' with the A2
design", `NEXT-SESSION.md:113-114`). An equally defensible reading, the state
*before* the newest accepted proposal, gives different answers in all three
rows. The choice is a design decision, not a default.

The pinned test is not the obstacle. Neither parametrisation of
`test_freeform_diff_baseline.py` creates a committed proposal, so the default
leaves its `:131-133` assertions true **[code: test read; not run]**.

**Fixes to the plan.**

1. S-2 row: add the three-row table above to the question. Change "Safe to
   start" to "Safe to start; **merge waits for the S-2 answer**". The lane
   builds and tests on the default but is not merged on it.
2. §2 prior-attempt table: move P-B2 to "partly covered (CDA-02, pending
   S-2)".
3. §6 CDA-02 row, scope: add "`baseline_kind` is an optional field of
   `DiffResponse`. The web tool loop reports `request_start` or
   `last_accepted`; the planner route (`pipeline_planner.py:3822`) and
   `pipeline_commit.py:517, :593` report `request_start`; the MCP surface
   omits the key. The lane does not edit `pipeline_planner.py` beyond that one
   keyword. Both `tool_batch.py` seams (`:2254`, `:3248`) use the same
   baseline. The existing request-start assertions in
   `test_freeform_diff_baseline.py` are kept unchanged and new cases are
   added beside them."
4. §6 CDA-02 row, collisions: replace the cell (see B5).

### B2. CDA-01: hiding the input creates a dead end the plan does not mention

**[code]** `TutorialFreeformShell.tsx:68-76` `buildBlocker` returns "The
Composer hasn't built a pipeline yet. Ask it to continue in the chat." when
there is no pipeline. The only retry the S-1a default keeps is the
interrupted-request retry, which exists only when `composerProgress.phase ===
"cancelled"` (`ChatPanel.tsx:490-496`). After a failed compose, a text-only
reply, a withheld reply or a page reload, a learner in script mode has no
textbox and no retry, and the instruction on screen points at a control the
lane removed. This is P-B19a ("no Retry, only 'Ask it to continue in the
chat'"), which §6 lists as "Not scheduled" and `prior-attempt.md:135` marks
"adjacent, not covered". Today the textbox is the way out; CDA-01 removes it.

The S-1a claim itself holds **[code]**: `retryMessage(interruptedRequest.id)`
resends a stored message by id and sends no new content.

**Fixes to the plan.**

1. §6 CDA-01 row: add "Script mode must leave a resend-the-brief control in
   every state after the brief in which no pipeline exists and nothing is
   composing (failed compose, text-only or withheld reply, reload). Correct
   the `TutorialFreeformShell.tsx:73` copy to name that control. Add a Vitest
   case for the state. The control resends the stored brief by id and accepts
   no text." Move the first clause of P-B19a out of "Not scheduled".
2. S-1 row: quote `NEXT-SESSION.md:92` ("a tutorial-only suppression is not
   allowed") and say that hiding suggestion Apply in the tutorial is such a
   suppression, accepted by the later R16 decision (`work-packages.md:126-127`)
   because it is frontend-only. The plan currently omits the sentence, so the
   maintainer cannot see that the later ruling overrides it.

Invariant 2 itself is not breached **[code]**: ADR-031 line 20 and
`work-packages.md:169-171` scope the rule to the backend, `allowFork={false}`
is an existing frontend precedent, and the one backend edit is copy inside an
existing admission check.

### B3. §5 test scope misses the files that pin wave-1 lanes, and five lanes cannot each run it

**(a) Missing directories. [code]** The four directories do not include:

- `tests/unit/web/test_*.py` (90 top-level files). `test_interpretation_state.py`,
  `test_prompt_artifact_boundary.py` and `test_single_prompt_system_review.py`
  reference `vague_term_wiring_count` or
  `approved_prompt_artifact_hash_from_options` (CDA-04).
  `test_credential_boundary_inventory.py` is keyed on `boot_probe.py`,
  `tool_batch.py`, `tutorial_service.py`, `tools/sessions.py` and
  `tools/_dispatch.py` (CDA-01, 02, 04, 05). `test_credential_guard.py` is
  CDA-06's own file.
- `tests/unit/plugins/llm/test_approved_prompt_artifact.py` and the
  `multi_query` plugin tests (CDA-04 edits `plugins/transforms/llm/multi_query.py`,
  which is not under `src/elspeth/web`, so the rule's trigger does not fire
  for it at all).
- `tests/integration/web/composer/` (two files reference the CDA-04 gate;
  CDA-05's parity test lives there).
- `tests/unit/scripts/test_composer_wire_census.py`, `tests/unit/composer_mcp/`
  (CDA-02) and `tests/unit/elspeth_lints/` (`fixtures/fingerprint_baseline.json`
  names nine of the wave-1 source files).

The rule was copied from `NEXT-SESSION.md:201-203`, where it answered "the
author had not run the file that pinned the behaviour". On this base the
pinning files are elsewhere.

**(b) Capacity. [code]** The host has 24 CPUs, a CI shard and the baseline
run were live during this review (load average 11.96), and `addopts` carries
`-n 12`. Five lanes each running the sweep is five broad suites at once,
which AGENTS.md forbids ("run only one broad suite at a time"; "Do not launch
parallel full suites from separate agent sessions"). §5 tells each lane to run
it; §7 says one at a time. The plan does not say who runs it.

**(c) `tests/unit/architecture`. [code + report]** Nothing in
`test_web_landscape_mutation_fencing.py` carries a deselecting marker, so the
long proofs run in the default selection. `prior-attempt.md` §7 records a
7,200 s proof timeout and one test at 582 s in a worktree. The baseline
command uses `timeout 5400`. Running the directory per merge is workable;
per lane in parallel is not.

**(d) Deselected by default. [code]** `slow`, `stress`, `performance`,
`testcontainer`, `live_provider` and `timing_sensitive`. Inside the four
directories that removes at least `test_pipeline_planner.py:5813, :7280`,
`test_transcript_argument_order.py:293` and `test_routes_state.py:1095`. The
scaffold byte pin test (`test_pipeline_planner.py:1717`) is not among them.

**Fix to the plan.** Replace the §5 "Test scope" bullet with:

> **Test scope.** A lane worker runs only its lane test command and the files
> its re-anchor section names as pinned, with `-n 4` or fewer. The
> orchestrator runs the sweep, one at a time, at `verify` for each merge:
> `tests/unit/web/composer`, `tests/unit/web/execution`,
> `tests/unit/web/sessions`, `tests/unit/architecture`, the top-level
> `tests/unit/web/test_*.py`, `tests/integration/web/composer`,
> `tests/unit/composer_mcp`, `tests/unit/elspeth_lints`,
> `tests/unit/scripts/test_composer_wire_census.py`, and for CDA-04
> `tests/unit/plugins/llm` and `tests/unit/plugins/transforms/llm`. The
> failure set is compared with a base run of the same selection. The
> `timing_sensitive` ids in these directories are deselected by default; the
> wave-end gate's timing stage covers them.

Also extend the baseline in `.claude/lanes/baseline/` to that selection
before the first `verify`. It was at 75% of the four-directory selection when
this review was written; §7 and §8 should not treat it as available until its
exit code is recorded.

### B4. CDA-04: the lane may not change the attested prompt hash on its own judgement

**[code]** `approved_prompt_artifact_hash_from_options`
(`web/interpretation_state.py:2796-2842`) produces the value stored in a
node's `approved_prompt_artifact_hash` option. Stored values are compared with
a fresh derivation at `interpretation_state.py:1297`, `:1344` and
`composer/tools/transforms.py:1935`, and written at
`sessions/pending_interpretation.py:820` and `interpretation_state.py:3299`.
A change to the derivation makes every stored approval on a multi-query node
stop matching. That is a persisted-data compatibility decision under AGENTS.md
§ Project delivery posture, not a lane-level call.

The §6 row lets the lane "state precisely what the hash item requires … or
drop it". As written it may also implement it.

**Fix to the plan.** §6 CDA-04 row: replace the hash sentence with "The lane
does not change the derivation in `approved_prompt_artifact_hash_from_options`.
It reports, from the finding's wording and the code, whether a change is
needed and what stored approvals it would invalidate; the change, if any, is a
maintainer decision. `report.md` also states the effect of the new staging
rejection on stored states that already carry the wiring (the brief's
read-only query)."

### B5. Scaffold byte pin: the CDA-02 collision cell is wrong and the pin rule invites a merge conflict

**[code]** The pin measures `"tools": request["tools"]` as well as the
messages (`test_pipeline_planner.py:1711-1717`), and `diff_pipeline` is in the
planner palette. A reworded `diff_pipeline` description therefore moves the
pin whether or not `pipeline_composer.md` changes. The §6 CDA-02 collision
cell ("`skills/pipeline_composer.md` only if the refusal teaching changes")
understates this. CDA-04's reworded aid sentence moves the same pin. Both
lanes will also touch `test_tool_declarations.py:584-587` (CDA-02) or
`test_planner_authoring_aids.py` (CDA-04), which are disjoint.

The §5 rule is consistent with the recorded rulings, with two gaps:

- The 2026-11-10 ruling quoted in the test (`:305-307`) is "the cap is a
  trigger to review for waste, not a reason to cut; with no obvious cut, the
  measured increase is authorised". The rule omits the review-for-waste step.
- "Raise the pin" reads as editing `_SKILL_REWORK_0812_CANONICAL_BYTES`. Two
  lanes editing one constant conflict textually, and the second lane's number
  is wrong on the merged tree. The file's pattern is one named constant per
  increment, summed at `:325-338`.

"It has no headroom" is unmeasured: the sum includes
`_FIXED_SCAFFOLDING_WORKING_HEADROOM_BYTES`, and `wave3.md` says byte overruns
"are inferred from the test's constants and comments, not measured".

**Fixes to the plan.**

1. §5 pin bullet: cite `test_pipeline_planner.py:304-338` and `:1717-1727`.
   Replace the last three sentences with "Measure first; the headroom is
   unmeasured. If the pin trips, review the added text for waste. If there is
   no cut that keeps the meaning, add a new named constant for the lane (the
   file's pattern) with a dated comment giving the measured before and after,
   add it to the sum, and keep the exact-boundary and +1-byte controls. Do not
   edit another lane's constant. The second lane to merge re-measures on the
   merged tree. List the increment in `report.md`."
2. §6 CDA-02 collision cell: "`test_pipeline_planner.py` byte pin (certain if
   the tool description changes; shared with CDA-04), generated inventories,
   `skills/pipeline_composer.md:613-616`."

---

## Advisory findings

1. **§ intro: "253 commits later".** Measured 254
   (`git rev-list --count 1a7ff0e63..8f5ca4292`); `wave1.md` says 254. **[code]**
2. **§7 `.lanes/`.** AGENTS.md § Subagent Reporting says lane state goes under
   `.claude/lanes/<run>/`. Both paths are gitignored (`.gitignore:69, :71`),
   and the baseline is already under `.claude/lanes/`. Use one. **[code]**
3. **§5 "that worktree's own `.venv`".** Ambiguous between "each lane builds
   one" and "lanes use the integration worktree's". Say which. If shared,
   forbid `uv sync` and `uv pip install` from a lane, and note that
   `scripts/worktree-cleanup.sh --link-venv` links the main checkout's
   environment, which is the wrong one here. The integration `.venv` is a real
   directory. **[code]**
4. **Trust-tier bindings.** `config/cicd/enforce_tier_model/web.yaml` and
   `tests/unit/elspeth_lints/fixtures/fingerprint_baseline.json` name most
   wave-1 source files. Add to §5: a worker never edits a
   `judge_metadata_signature` or the fingerprint baseline to go green; drift
   is reported. `work-packages.md` §4 says only "read AGENTS.md". **[code]**
5. **CDA-02 `is_valid: false` item.** Under the S-2 default the no-baseline
   branch is reachable only on MCP or when `turn_evidence` is `None`.
   `_saved_graph_rejection_result` and `_prepend_rejection_entry`
   (`tools/_common.py:1619, :1654`) are shared by other rejections. Tell the
   lane to change the result for this code only, not the shared helpers, or
   to report it as not done. **[code]**
6. **CDA-06.** State which copy an unknown surface gets (today's, so a new
   surface fails toward the existing wording). The three literal-copy pins
   (`test_routes_state.py:2809, :2912`, `test_service_pricing_identity.py`)
   are on user surfaces and stay. No collision with another wave-1 lane.
   **[code for the pins; default is a recommendation]**
7. **CDA-05.** After this lane the hatch probe carries the `diff_pipeline`
   declaration that CDA-02 rewords. Tell the lane to compute expected tools
   and strict counts from `planner_tool_definitions(...)`, not literals, so
   the CDA-02 merge cannot break it. **[inferred: the tests are not written]**
8. **Blocked lanes and wave boundaries.** "A wave starts from the tree that
   contains the earlier merges" does not say whether wave 3 waits for CDA-18,
   CDA-19 and CDA-07b, which are blocked on S-4 and S-5. Say that blocked
   lanes float and are re-collided when unblocked. CDA-18 edits
   `tool_batch.py` and `provider_gateway.py` (CDA-22, CDA-20, CDA-17);
   CDA-07b edits `sessions/routes/_helpers.py`, `planning_application.py` and
   `composer/service.py` (CDA-09, CDA-22, CDA-20). **[report]**
9. **CDA-16 after CDA-07b,** or re-measure: the carry-forward changes the
   advisor history that the cache investigation measures. **[inferred]**
10. **P-B1 has no brief.** The plan sequences it but nothing defines it. The
    brief must say what the audit record holds (raw or decoded arguments) and
    whether the 0.8.12 argument-shape instruction is then removed. Its place
    after CDA-10 is acceptable. **[inferred]**
11. **P-B17** is "open as its own item" (S-1) but appears in no wave and not
    under "Not scheduled". Same for the P-B11 lane (S-8). List them.
12. **S-9.** Lanes read the CDA brief from another worktree. Confirm the
    Codex worker's sandbox can read
    `.claude/worktrees/composer-desk-audit/`, or copy the lane's brief into
    its lane directory. **[not checked]**
13. **CDA-01 frontend.** Each lane worktree needs `npm ci` before Vitest.
    With Apply hidden, say whether the suggestion text itself is still shown.
14. **Commits.** Add `scripts/branch-safety-check.sh --intent commit` before
    each lane commit (AGENTS.md § Canonical scripts).

---

## Answers to the six questions

### 1. Fidelity

§2 rows for CDA-01, 02, 04, 05, 06, 09, 20 and 22 match their reports. No row
softens a report finding. Omissions that matter are B1(a), B2 and B4; the
reports missed the first two as well.

Code claims checked, all **[code]**:

| Claim | Result |
|---|---|
| `tool_batch.py` passes a request-start baseline | True at `:2254` and `:3248`. Incomplete: see B1(a). |
| `test_freeform_diff_baseline.py` pins the next-request reset | True, `:131-133`. |
| `credential_guard.py:52-67` ignores `surface` for copy | True (`:57`, `:63`). |
| `boot_probe.py` hatch sends three tools | True, `:210-218`: two recovery tools and the terminal. |
| Production hatch sends the full palette | True, `pipeline_planner.py:4663-4666`; `planner_tool_definitions` at `:1741-1760`. |
| `planner_authoring_aids.py:889-897` holds the sentence | True; "Reviewed slots belong in the node-level template." at `:892-893`. |
| CDA-01 affordance locations | `ChatInput` at `ChatPanel.tsx:1323-1335` with `onCancel` at `:1326`; retry at `:1308-1310`; `BlobManager` at `:1317`. True. |
| `committed_state_id` and its CHECK | `sessions/models.py:706, :738`. True. |
| `composer_provider_thinking` surface | `provider_thinking.py:62`. True. |
| 429 falls through as non-retryable | `provider_errors.py:153`, the final `return … False`. True; the plan's `:158` is a few lines off. |
| AWS discovery limit is 8 | `deploy/aws-ecs/terraform/modules/scenario/locals.tf:474`. True. |
| `99bd0a7e1`, `a2ad64974`, `a33cbb6d6` are on the base | True (`git merge-base --is-ancestor`). |
| None of the wave-1 files is in `reviewed-full-source-guards.json` | True (zero hits; the instrument matched `sessions/service.py` as a positive control). |

### 2. Defaults

| Default | Finding |
|---|---|
| S-1 | Safe to start. Reversible, frontend plus copy, leaves the admission rule alone. Needs the disclosure in B2 fix 2. |
| S-1a | Safe as far as it goes; the claim is true in code. Incomplete: B2. |
| S-2 | Implementable on this base: `list_composition_proposals` (`sessions/service.py:4785`) and `get_state` are already reachable from the dispatch closure (`tool_batch.py:3294-3300`), and the pinned test survives. Not coherent as a fix for P-B2, and it pre-empts the definition the maintainer deferred: B1. Start, do not merge. What auto-commit sessions hold in the proposal table is still unverified; the plan correctly makes the lane check first. |
| S-5 | Safe. CDA-07a does not touch the "never re-enter" contract (`withheld_replies.py:12`). Wave 2 is re-planned anyway. |

### 3. Wave 1 collisions

No two wave-1 lanes must edit the same source file **[code: file lists
recomputed against the tree]**. Shared artefacts:

- `test_pipeline_planner.py` byte-pin constants: CDA-02 and CDA-04 (B5).
- Generated skill inventory and wire census: CDA-02 and CDA-04. The plan's
  "regenerate, never hand-merge" covers it.
- Per-file inventories (`config/cicd/soft-mapping-census.yaml`,
  `contracts-whitelist.yaml`, `enforce_tier_model/web.yaml`,
  `test_credential_boundary_inventory.py`): keyed by file or qualified name,
  so entries are disjoint; a lane trips one only by adding a new
  `dict[str, Any]`, free-text request field or tier finding.

CDA-04 then CDA-02 is sufficient with the B5 re-measure rule. What §5 does not
prepare a lane for: B3(a), B4, advisory 4 and advisory 7.

### 4. Later waves

The serial chains match the reports' collision recomputes: CDA-07a → 08 → 09
on the advisor and completion files; wave 3 serial on `pipeline_planner.py`
and the byte pin; CDA-21 → 23 on pin files; CDA-22 → 20 on
`composer/service.py`. Gaps are advisory 8, 9 and 10. `wave3.md` recommends
that each wave-3 lane add its own named increment and CDA-12 re-baseline; B5
makes §5 say the same.

### 5. §5 rules

- Scaffold pin rule: consistent with the recorded ruling, incomplete (B5).
- Test scope rule: wrong coverage and unworkable in parallel (B3).
- Base and environment: correct; ambiguous on which `.venv` (advisory 3).
- Closed contracts, settings fixtures, way out, no changelog edits: correct
  and consistent with AGENTS.md.
- No rule conflicts with the composer invariants.

### 6. Anything else

- §8 should add: the byte pin's headroom is unmeasured; whether auto-commit
  sessions hold committed proposal rows is unverified; the baseline run had
  not exited.
- Nothing else found that blocks wave 1.

---

## Not verified by this review

- No test, suite, browser walk or provider call was run.
- Whether auto-commit edits create `status='committed'` proposal rows.
- Whether the byte pin trips for any given rewording.
- The bodies of wave 2 to 4 report sections beyond their overlap and
  cross-lane parts; later-wave findings above rest on those parts.
- Codex sandbox access to the `composer-desk-audit` worktree.
