# issue327-light, phase 2 report: U-13/U-14 rest, MCP F3, U-17

> **RESUME TASK 0 (hung tests) is the release's FIRST priority, owned by u45-firstcompose plus one coordinator-assigned owner. It is NOT this lane's job.** See `.claude/lanes/release-0.8.10/RESUME-FIRST.md`. This lane resumes its own unit, under the guardrails: one pytest at a time, `-n 3` max, `timeout 2400`, both hang files `--ignore`d, no directory runs without a slot. Do not touch the fencing gate, the reserve-contract tests or the pytest config. Maintainer's order, 2026-10-25.

> **Coordinator corrections (10-25, from COLD-START-REVIEW.md). These override the text below:**
> - O2: f3 `c153f5bb0` and u17 `98b090715` are contained via the target's TC-006 fold (`9aff4e441`), not via the rest branch. Their only `git cherry` residue is the two pre-fold TC-006 commits.
> - N4: `rebind.py` and `status_pass.py` lived only in the coordinator's session scratchpad. They are copied to `docs/release/0.8.10-handover/instruments/issue327/`.


Lane branch `codex/0.8.10-issue327-rest`, worktree `.claude/worktrees/issue327-rest-20261025`.
Rebased with `git rebase --onto release/0.8.10-assembly 9df1d29e6` onto the target **d7ad18596**.
The pre-rebase tip is pinned at `preserve/0.8.10-issue327-rest-pre-target-rebase` (98b090715).
TC-006 landed earlier (9aff4e441, ef951924a, 729461151, 33094bb63, 0f1ca2274) and is not on this branch.

## Commits in landing order (on d7ad18596)

| Commit | Unit | What | Failing-first evidence |
|---|---|---|---|
| 06ef4563b | TC-001 | WIP: failing-first attachment-root tests | red-first; **fold into 91064f603** |
| 91064f603 | TC-001 | bind named and plural attachment roots in set_pipeline | `test_set_pipeline_attachment_roots.py::test_set_pipeline_authors_named_attachment_roots` |
| 81acdb73d | TC-005 | WIP: failing-first projection tests | red-first; **fold into 51d66f699** |
| 9285a2d3c | TC-005 | WIP: failing-first reviewed plural revalidation tests | red-first; **fold into 51d66f699** |
| 51d66f699 | TC-005 | project attachment custody to replacement selectors | `test_attachment_authoring_projection.py::test_plural_authoring_projection_round_trips[scans]` |
| 9c8a4c6b9 | U-13 | disclose the effective 512-id limit (superseded in prose by 04641f5ee) | |
| 1c767f4a3 | U-13 | provider-authored attachment roots are the only structure (retention test) | retention control |
| 04641f5ee | U-13 | 512 ruling: advertise the detector's bound from one constant | `test_advertised_blob_ids_bound_is_the_credential_width` (+ mutation control) |
| 073fd6f3f | U-13 | re-pin the inline CSV comparison read moved by 04641f5ee | **lands with 04641f5ee** (04641f5ee alone is red at that pin) |
| 2d1bbe1d8 | Astra #1 | refuse plural custody the resolver could not have written (fix + tests in one commit) | `test_contradictory_stored_plural_custody_is_round_trip_unavailable` (4 cases red, control green) |
| 6a41f5978 | Astra #2 | reviewed plural reuse end to end (tests only) | 4 mutations killed |
| fb92d8bfb | Astra #3 | attachment-root retention through the real SDK gateway (tests only) | server-side reorder mutation killed |
| 46b16bdde | Astra #4 | remaining mutation controls (tests only) | 6 mutations killed |
| cf3c2aab0 | Astra #5 | §8.1 boundary fixtures (tests only) | 8 mutations killed |
| 6cb9a99ca | F3 | audit an unregistered tool name by digest, never echo it (fix + test in one commit) | `test_argument_admission_audit.py::test_an_unregistered_tool_name_is_audited_by_digest_and_never_echoed` |
| f0e1eb653 | U-17 | carry the wire_secret_ref dossier and its test (`cherry-pick -x b6d5e2649`) | test passes on the target tree |
| a81198c0b | U-17 | WSR-C4-SELECTOR conformant after TC-003 | |
| 8acc37796 | U-17 | carry the MCP session dossiers, docs only (from bd1407afc, 85f1a579c, 9799c19ac, 7a82c42f4) | test files not re-carried |
| ce904993a | F3 | keep unregistered tool names out of the SDK's log records (Daybreak Medium; fix + test) | same test id, now with a newline case; 4 mutations killed |
| 27fb378b5 | Astra #3 | A9 per-transition ledger for the freeform and tutorial requests | |
| 0031a2fca | 512 | comment: why the bound is not `_MAX_BLOBS` | |
| 1160935e1 | U-17 | dossier text describes the fixed TC-018/019/020 behaviour | |
| 20408a1ec | U-17 | bindings re-derived on the 0.8.10 tree | 129 contract tests passed, exit 0 |

**Landing rule.** Three pre-rule WIP commits add tests ahead of their fix: 06ef4563b, 81acdb73d and 9285a2d3c.
- The per-commit check, stopped by the pause after its first commit, measured 06ef4563b at exit 1 (10 failed, 2 passed), as expected for a red-first commit. Log: `.claude/worktrees/issue327-rest-20261025/.claude/lanes/issue327-rest/landcheck/`.
- Fold map for landing: 06ef4563b into 91064f603; 81acdb73d and 9285a2d3c into 51d66f699. 04641f5ee and 073fd6f3f land as one unit.
- Every commit made after the rule (2d1bbe1d8 onward) keeps any failing-first test inside its fix commit.

## U-13 Astra review: findings and dispositions

The one Astra review is at `.claude/lanes/issue327-rest/astra-u13.md` (verdict: request changes).

1. **Medium, real gap, fixed (2d1bbe1d8).** The projection returned `blob_ids` for plural custody with a duplicate content hash, or mixed with scalar custody markers. Both now report `round_trip_unavailable`. The entry check mirrors the plugin's own two uniqueness rules. The helper is typed on the owned `SourceSpec`, so the soft-mapping census does not move.
2. **Medium, test gap, closed (6a41f5978).** The positive fixture now carries `observed_columns` and `sample_rows`. Reuse goes through set_pipeline with and without authority; the two refusal layers are a verbatim copy refused at the guarantee stamp and a stamp-stripped copy refused as manual blobs. A commit-level pair runs through `prepare_pipeline_proposal_commit`: unchanged entries bind, and a changed later entry refuses before dispatch with no state published.
3. **Medium, test gap, closed (fb92d8bfb, 27fb378b5).** Raw replies go through `litellm.acompletion` via `ComposerSDKScript` and `service.compose`. The final state must equal the provider's single transition. The A9 ledger asserts provider, set_pipeline, provider for the freeform and the tutorial brief in both dialects.
4. **Low, closed (46b16bdde).** MIME freshness, session move, conflicting owned selectors, mixed blob_ref/source_authoring, and inline creation with null selectors.
5. **Low, closed (cf3c2aab0).** Plugin 1,000 accepted, filename 1/512/513, size 0, default credential thresholds at and beyond each bound with a planted-token control, and the 49/50/51 inventory with an omitted older id bound directly. This exposed a test-helper defect: blob_rows' `_entry` gave ids 10 and 170 the same payload_ref.

The scaffold tripwire passes on the rebased tree, so no repricing was needed.

## MCP F3

- **Code:** the audit `tool_name` is the raw name only if registered; otherwise it is `<unregistered>:sha256:<digest of UTF-8 bytes, surrogatepass>`. The digest lives inside `tool_name`, so no new audit field or schema change was needed. The Unknown tool response no longer repeats the name.
- **Astra** (`.claude/worktrees/issue327-f3-20261025/.claude/lanes/issue327-rest/astra-f3.md`): approve, no new defect.
- **Daybreak** (`.../daybreak-f3.md`): the handler is clean on every exit path, registered-set membership is exact, and both reverts are killed. It confirmed one Medium: the MCP SDK (1.30.0) logs the raw unregistered name at WARNING before ELSPETH's handler runs. It reaches stderr through the last-resort handler, and a newline splits it into injected lines. **Fixed in ce904993a** with an exact-template filter on the SDK server logger, so a changed SDK message fails the test rather than leaking quietly.
- **Pre-existing, reported, not fixed (out of F3 scope; needs an owner):** an uncanonicalizable argument key such as a lone surrogate makes the canonicalization sentinel itself fail before the audit `try`. The call returns an error with zero audit rows. This reproduces on the base too (Astra P2).
- **Not acted on:** Daybreak's hypothesis that the SDK's generic `str(exc)` framing could echo a future exception that embeds the name. No current code constructs one.

## U-17

- The wire_secret_ref dossier is carried with `-x`. WSR-C4-SELECTOR is conformant: the TC-003 claim is TESTED_PASS, citing `test_secret_rejection_contracts.py::test_target_id_description_states_the_source_selector_contract` and the declaration byte pin.
- The four MCP session dossiers are carried docs-only; the five test files were not re-carried. Their text describes the fixed TC-018/019/020 behaviour, citing the current (renamed) tests. Every cited test function exists, checked with an instrument that flags a known-missing old name.
- Source line locators are mapped mechanically to this tree: same text inside the same enclosing symbol, one resolved by hand.
- Review-receipt paragraphs pointed at `reviews/*.md` records that are not in the repository. Following the list_secret_refs precedent, each is replaced by a short review status.
- Bindings are re-derived at 1160935e1 with the preserved `scripts/tool_contracts.py` from 1530e7610 (sha256 f1ccc5322e0f808d…, run from the scratchpad, not committed). The body, assertion and raises rules reproduce the audited entries exactly.
  - All symbols resolve and all guidance paragraphs still match.
  - The wire interface snapshots come from the live builders and differ from the audited ones only by the U-04 description.
  - A second pass shows no drift, and a tampered hash is detected.
  - `source_commit` names lane commit 1160935e1. The bound blobs are identical to what lands, but the SHA changes when cherry-picked.
- **Ceremony flag for the maintainer:** these binding companions are hash manifests over documentation, the kind of artifact AGENTS.md § Project delivery posture discourages. Removing them would be a marginal call, so I kept and rebound them and surface the tradeoff here.

**U-17 Astra review: not completed.** It was started at about 06:42 and stopped by the pause order before it produced output. Partial log: `.claude/worktrees/issue327-rest-20261025/.claude/lanes/issue327-rest/astra-u17.log`. The one U-17 Astra review is still owed.

## Gates measured before the pause (tip 0031a2fca, base d7ad18596)

The bound code is identical at 0031a2fca and 20408a1ec; everything after it is documentation. Logs are under `.claude/worktrees/issue327-rest-20261025/.claude/lanes/issue327-rest/gates-0031a2fca/` and `gates-d7ad18596/`.

| Gate | Result |
|---|---|
| ruff check, ruff format --check, mypy src/elspeth, check_contracts, masquerade seed --check | all exit 0 |
| Keyless trust-tier corpus set-diff | **+2, -0.** Both are R5 `isinstance` in U-13 code: `web/composer/reviewed_source_authority.py:79` (`_reviewed_plural_entries`) and `web/composer/tools/sessions.py:1152` (`isinstance(bound_options, SetPipelineCandidate)`). **Owed:** fix at the producer, for example `type(x) is SetPipelineCandidate` (house style), and parse the persisted list honestly. |
| Directory pytest set-diff, tip vs base | **not completed**: stopped at about 9% by the pause and the new capacity rules. Owed. |
| Focused on the rebased tree | 568 passed (U-13, F3 and §8.1 files plus the commit-level lifecycle test); composer_mcp 285 passed; contract files 129 passed |
| Planner tripwire, Sessions authority, redaction snapshot (rebased tree, mid-edit) | the scaffold tripwire passes. Sessions shows only the 2 base failures (preferences facets, receipt revert/fork) plus the approved XFAIL. 5 `test_pipeline_planner.py` failures (escape-hatch/discovery-batch/attempt-trail) still need to be checked against the base; they look like the target's planner-route-retirement reds. |

## COLD START (paused 2026-10-25)

**Units owned:** U-13/U-14 (TC-001, TC-005; TC-004 and TC-006 already landed), MCP F3, U-17. Lane `issue327-light`.

**Branches @ tip, all clean:**
- `codex/0.8.10-issue327-rest` @ **20408a1ec**, worktree `.claude/worktrees/issue327-rest-20261025`. **This is the only live branch**: all U-13, F3 and U-17 work, rebased on target d7ad18596.
- Superseded, kept for history, fully contained in the rest branch: `codex/0.8.10-issue327-f3` @ c153f5bb0; `codex/0.8.10-issue327-u17` @ 98b090715; `codex/0.8.10-issue327-tc006-landing` @ 9df1d29e6 (already landed). `preserve/0.8.10-issue327-rest-pre-target-rebase` @ 98b090715.
- Helper worktrees, detached and clean: `issue327-rest-base-20261025` @ d7ad18596 (gate base); `issue327-landcheck-20261025` @ 91064f603 (landing check, stopped). `issue327-light-20261024` is the phase-1 lane, already landed.

**Done and verified** (evidence paths are in the sections above):
- TC-001 and TC-005 implemented.
- U-13 Astra findings 1–5 closed.
- The 512 ruling, plus the `_MAX_BLOBS` comment.
- MCP F3 code plus the SDK log filter, with one Astra (approve) and one Daybreak review.
- U-17 carry, TC-003 flip, the TC-018/019/020 text update (complete in 1160935e1) and the binding rebind.

**In progress / half-done:** nothing is half-written; there are no WIP commits after the rule.

**Next steps on resume:**
1. Rebase onto the then-current target, pinning the tip first:
   ```
   git -C .claude/worktrees/issue327-rest-20261025 branch preserve/0.8.10-issue327-rest-pre-resume HEAD
   git -C .claude/worktrees/issue327-rest-20261025 -c rerere.enabled=false rebase --onto release/0.8.10-assembly d7ad18596
   ```
   The `--onto <target> 9df1d29e6` form was already applied; the base is now d7ad18596.
2. Re-run the U-17 binding rebind if the target changed any bound source. It uses `scripts/tool_contracts.py` from `git show 1530e7610:scripts/tool_contracts.py`, kept in the scratchpad and never committed, and the rebind/status scripts described in 20408a1ec's message. A second pass must report `CHANGED 0`.
3. Fix the two R5 keyless findings above at their producers, then re-run the keyless set-diff against the target.
4. With a slot from the coordinator, run the directory set-diff under the new hard rules: one pytest, `-n 3`, `timeout 2400`, and `--ignore` both `tests/unit/architecture/test_web_landscape_mutation_fencing.py` and `tests/unit/architecture/test_web_reserve_reviewed_contracts.py`. Set: `tests/unit/web/composer tests/unit/architecture tests/unit/contracts tests/unit/plugins/sources tests/integration/web/composer tests/unit/composer_mcp`, tip vs target.
5. Run the one owed U-17 Astra review (brief in `astra-u17.log`'s command line, recorded above).
6. Re-run the per-commit landing check that the pause stopped after its first commit. The script is described in the landing-rule paragraph; it ran each commit's changed test files in the `issue327-landcheck-20261025` worktree. Use one pytest at `-n 3` under `timeout 2400`.
7. Hand the coordinator the landing list. Fold the three red-first WIP commits (map above). **9b7a5a7b6 and 0870e6f2a, now 04641f5ee and 073fd6f3f, land together.**

**Open questions and rulings owed:**
- **Coordinator: unowned audit defect.** An uncanonicalizable MCP argument key, such as a lone surrogate, makes the canonicalization sentinel fail before `call_tool`'s audit try/finally. The call returns an error with **zero audit rows**. It is pre-existing and reproduces on the base; it was found by the F3 Astra review (P2) and is out of F3 scope. It needs an owner.
- **Stopped runs (pause), each listed in the next steps:** the directory gate set-diff (step 4), the U-17 Astra review (step 5) and the per-commit landing check (step 6). Partial logs are under `.claude/worktrees/issue327-rest-20261025/.claude/lanes/issue327-rest/` (`gates-0031a2fca/`, `gates-d7ad18596/`, `astra-u17.log`, `landcheck/`).
- **Maintainer:** whether the dossier binding companions (hash manifests over docs) are worth keeping, per AGENTS.md delivery posture.

**Hazards:**
- `source_commit` in the five bindings names lane commit 1160935e1, which changes on cherry-pick. The bound blobs are what land.
- The MCP SDK log filter matches exact SDK message templates. An `mcp` upgrade that rewords them fails `test_an_unregistered_tool_name_is_audited_by_digest_and_never_echoed`, by design.
- The 5 planner failures need base attribution before anyone reads them as lane regressions.
- KNOWN HANG: `test_web_landscape_mutation_fencing.py` (exclude it).
