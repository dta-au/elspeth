# Protocol build report: U-41, U-42, U-40 (partial), U-43

> **RESUME TASK 0 (hung tests) is the release's FIRST priority, owned by u45-firstcompose plus one coordinator-assigned owner. It is NOT this lane's job.** See `.claude/lanes/release-0.8.10/RESUME-FIRST.md`. This lane resumes its own unit, under the guardrails: one pytest at a time, `-n 3` max, `timeout 2400`, both hang files `--ignore`d, no directory runs without a slot. Do not touch the fencing gate, the reserve-contract tests or the pytest config. Maintainer's order, 2026-10-25.

> **Coordinator corrections (10-25, from COLD-START-REVIEW.md). These override the text below:**
> - W5: the U-44b reds file is `tests/unit/web/sessions/test_continuation_prompt_producers.py`, NOT `tests/unit/web/composer/...`. That wrong path is the same one that made the residue landing check exit 5.
> - S2: the attachment-application lease question is CLOSED. Every line of the `-full` patch is in U-35b's `a8aeab905`/`4c5e08d2d`; only line coordination at landing is left.
> - I3: `residue/landcheck/run.sh` uses `-n 4` with no timeout. Change it to `-n 3` under `timeout 2400` with both hang files ignored before running it.


**Branch:** `codex/0.8.10-protocol`, worktree `.claude/worktrees/protocol-20261025`.
**Base:** the target `release/0.8.10-assembly` at `d7ad18596` (U-38). Landed rebase on 2026-10-25: old tip `247b7f5af` pinned at `preserve/0.8.10-protocol-u41-pre-rebase`. **Tip:** `0cfa7d7a3`.
**Design:** `docs/architecture/design-notes/0.8.10-continuation-protocol-design.md` on `codex/0.8.10-design-protocol`.

## State per unit
| Unit | State |
|---|---|
| U-41 (C7 server) | Built and reviewed. Lifecycle-2 Stop refuses explicitly (409 `composer_continuation_stop_unavailable`, no state change), pinned by `test_lifecycle2_stop_refuses_explicitly_and_changes_nothing` (control MS1 killed); the real v2 Stop replaces it when U-35b's `request_cancel` lands, before the 0.8.10 merge (part of U-41). D-2 replay is the runtime's (U-35b `_replay_request_hash`). |
| U-42 (C8 client) | Built, reviewed, and W-1 Playwright spec added. Not added: the "Changes saved before the stop remain in the pipeline" line (needs the runtime's R8 saved-revision attribution). |
| U-40 (C6 control) | Step 1 only (pure `checkpoint_and_continue` module, codes). Palette call sites and the driver seam wait on the runtime's U-37 yield API; interface proposal sent to design-runtime, no reply yet. Advisor follow-through (K-7) not started. |
| U-43 (S7 check) | Run; see below. |

## Commits (`git log d7ad18596..HEAD`, newest first)
- `0cfa7d7a3` fix(composer): an audit-integrity failure crashes the stream instead of closing it quietly
- `9ad6091d0` fix(composer): the close path withholds an expired reconnect frame and catches only its own timeout
- `774d95f62` refactor(composer): send the drain-time reconnect frame inside the one close path
- `dde1d556d` test(composer): P-5 — a v2 stream resumes on another replica from SQL alone
- `27a68ef82` fix(composer): lifecycle-2 Stop refuses explicitly until the runtime's v2 request_cancel lands
- `65c2b5e05` test(composer-ui): W-1 continuation observer in a real browser; announce a successor first seen queued
- `ad28ff99a` fix(composer-ui): U-42 Astra review — v2 observer loop, ownership, Stop scope
- `9d654d238` feat(composer): the checkpoint_and_continue control, pure parts (U-40 step 1)
- `d08ff2ed2` feat(composer-ui): Continuation observer UX — phase copy, successor notice, Stopping, discovery (U-42 step 3)
- `508850069` fix(composer-ui): choose the submission version at the one submit choke point; A9-T (U-42 step 2)
- `31b52c41e` feat(composer-ui): Composer-Operation-Version client and the v2 observer (U-42 step 1)
- `3741ccd63` fix(composer): U-41 Astra review — consistent reads, bounded lifecycle selection, drain reconnect
- `5019d2e38` feat(composer): the v2 admission acknowledgement (U-41 step 5)
- `40736009a` feat(composer): discover the live Composer operation without custody (U-41 step 4)
- `a413433c6` feat(composer): the v2 SSE stream with state-transfer resume and the reconnect frame (U-41 step 3)
- `5f1005eb3` feat(composer): row-lifecycle dispatch and the v2 operation snapshot (U-41 step 2)
- `db005a63d` feat(composer): negotiate Composer-Operation-Version on the durable routes (U-41 step 1)

## Landing on d7ad18596 (U-38)
1. Pinned `preserve/0.8.10-protocol-u41-pre-rebase` at `247b7f5af`.
2. Replayed the series onto `d7ad18596` by cherry-pick. The planted-mutation fix "a missing Composer-Operation-Version means 1" (`44f56e823`) is folded into U-41 step 1 (`db005a63d`), so no landed commit carries the mutation.
3. Deleted my duplicate `composer_continuation_enabled` field and the startup refusal; config.py is byte-identical to the target. The route still passes U-38's setting to the authority's `continuation_enabled`. The refusal test B-13 is replaced by `test_system_status_advertises_version_two_when_the_gate_is_on`, now possible because U-38 builds the policy whenever the gate is on (control MB13 killed). Two conflicts were resolved by hand: config.py (kept the target) and the step-2 test file (kept the new B-13 and appended step 2's tests).
4. Landing rule, measured per commit with JUnit pairs (test id, first line of the message, `/tmp` paths and uuids normalised). Parser control: it counts 18 failures in a file known to hold 18.

| Check | Result | Log (`landcheck/`) |
|---|---|---|
| Backend selection (sessions/routes, composer_stream, continuation_control, error_code_redaction, schemas, test_app, check_contracts, composer battery) at the base and at each of the 12 backend commits | 0 failing at every commit (691 tests at the base, 796 at the tip); 0 new pairs; every new id passes at its introducing commit | `<sha>.xml`, `exits.txt` |
| Full vitest at the base and at each of the 5 frontend commits | exit 0 at every commit, 0 failures (4456 tests at the base, 4504 at the last) | `v-<sha>.xml`, `vexits.txt` |

5. Evidence re-run on the rebased tip `0cfa7d7a3`:

| Check | Result | Log |
|---|---|---|
| A9 `test_tutorial_routes.py` | 0, 18 passed | `a9-tutorial-routes-rb.log` |
| A9 `test_accept_refuses_server_provider_proposal_provenance` | 0, 1 passed | `a9-provider-server-rb.log` |
| A9 Playwright `tutorial.spec.ts` | 0, 3 passed | `pw-rb-tutorial.spec.ts.log` |
| W-1 `composer-continuation-observer.spec.ts` | 0, 1 passed | `pw-rb-composer-continuation-observer.spec.ts.log` |
| W-2 `composer-workspace-accessibility.spec.ts` | 0, 8 passed | `pw-rb-composer-workspace-accessibility.spec.ts.log` |
| A9-T and `transition-ledger.test.ts` | inside the full vitest run at `65c2b5e05`; no frontend file changes after it | `landcheck/v-65c2b5e05.xml` |
| Added `provider=server` lines in `src` | 0 (control line counts 1) | inline |
| Added lines mentioning "tutorial" in `src` | 0 in backend Python. Frontend: 2 comments saying freeform and tutorial share one path, plus the A9-T test and the W-1 spec. No tutorial branch. | inline |
| Keyless multiset diff, `d7ad18596` vs `0cfa7d7a3` | 1 re-keyed: the `finish_response` close handler, from the 4-tuple to `except TimeoutError:`; nothing else added or removed | `keyless-d7ad.log`, `keyless-ptip.log` |

## Evidence (logs under `.claude/worktrees/protocol-20261025/.claude/lanes/protocol/`)
| Check | Exit / result | Log |
|---|---|---|
| Backend unit + integration (routes/composer, stream suites, DTOs, test_app, tutorial routes, U-40 control) on the final rebased tree | 0, 628 passed | `rebased2-backend.log` |
| Frontend vitest, full, on the final rebased tree | 0, 4504 passed (one earlier run: 5 timeouts at host load 54; the four files pass alone, 203 tests) | `rebased2-vitest.log`, `u42-1-rerun.log` |
| tsc, eslint on changed frontend files | 0, 0 | inline |
| ruff, mypy on changed Python modules | 0 | `ruff*.log`, `mypy2.log` |
| Mutation controls | U-41: 37 (M1–M28, MR1–MR8, M12/M22 after strengthening); U-42: 35 (FM1–7, AM1–2, UM1–7, RM1–13, RM10b, WM1); U-40: 4 (CM1–4). All killed. | `mut-*.log` |
| `scripts/branch-safety-check.sh` before every commit and the rebase | 0 | `bsc-*.log` |
| A9: `test_tutorial_routes.py` | 0 (18 passed) | `a9-tutorial-routes.log` |
| A9: `test_accept_refuses_server_provider_proposal_provenance` | 0 | `a9-provider-server.log` |
| A9: `transition-ledger.test.ts` | 0 (14 passed), re-run after U-42: 0 | `a9-transition-ledger*.log` |
| A9: `tutorial.spec.ts` (Playwright) | 0 (3 passed) before U-42 and 0 (3 passed) after the U-42 review fixes | `a9-tutorial-spec.log`, `a9-tutorial-spec-u42.log` |
| W-2: `composer-workspace-accessibility.spec.ts` (Playwright, after U-42) | 0 (8 passed) | `w2-accessibility.log` |
| W-1: `composer-continuation-observer.spec.ts` (Playwright, new) | 0 (1 passed); control WM1 killed | `w1-continuation.log`, `mut-w1.json` |
| A9: added `provider=.server` / `tutorial` lines in `src` | 0 / 0 (control: a planted line counts 1) | inline |
| A9-T: tutorial negotiates the same version as freeform | `TutorialFreeformShell.version.test.tsx`: real store→fetch path, versions 1 and 2; AM1/AM2 killed | `mut-u42-a9t*.log` |
| Astra review U-41 | 9 findings, all verified and adopted (`284b2e605`) | `astra-u41-review.md` |
| Astra review U-42 | 13 findings, all verified and adopted (`7e1b91528`) | `astra-u42-review.md` |

## Keyless trust-tier set-diff and the composer_stream.py decision (lead request (b))
**Method.** The earlier base set (`keyless-base.log`) came from an older tree: two `advisor_transcript.py` lines differed although the file is byte-identical at base and tip. I rescanned the true base `ba4b25ee2` in a detached worktree and compared it with the tip as a multiset, with line numbers removed and duplicates kept. A plain set hid a second identical `except ComposerStreamAuthorizationDenied:` finding. The multiset diff exposed it, and it is now gone. Logs: `keyless-base3.log`, `keyless-tip5.log` (both exit 1 with the usual corpus).

**Result at `247b7f5af`.** One finding re-keyed, none added:
- Removed: `composer_stream.py` R6 `except (TimeoutError, SQLAlchemyError, AuditIntegrityError, AsyncWorkerAdmissionTimeoutError) as exc:` (the base key).
- Added: `composer_stream.py` R6 `except TimeoutError:` (the same `finish_response` close handler, narrowed).
- The `except TimeoutError` that U-41 added around the reconnect send was removed in `caa435e3a`.

**Decision per finding.**
1. *U-41's separate reconnect-send handler.* **Fixed** by removal (`caa435e3a`). That change introduced a defect, fixed in `247b7f5af`. The drain-time reconnect frame goes through `emit`, which raises `ComposerStreamAuthorizationDenied` once authorization has expired. Nothing on the close path caught it, so the exception escaped after the headers were sent. At the close nothing remains to abort, so expiry is now a decision: the frame is withheld and the closing chunk is still sent. Test: `test_drain_after_authorization_expiry_closes_without_the_reconnect_frame`. Controls MX1, MX2, MX4 and MC1 killed.
2. *The pre-existing bounded-close handler in `finish_response`.* **Fixed in part, then justified.** Its tuple listed SQLAlchemyError, AuditIntegrityError, AsyncWorkerAdmissionTimeoutError and, after U-41, RequiredGenerationUnavailable. The close path only sends an empty chunk and an optional stateless reconnect frame (`reconnect_frame()` builds a DTO and encodes it, with no SQL and no executor). So none of those can be raised there. AsyncWorkerAdmissionTimeoutError is in any case a TimeoutError subclass. The handler now catches only the `TimeoutError` that `bounded` raises (control MX3 killed). The remaining `except TimeoutError:` is the honest form. The *client* did not take the final chunk within the send deadline. That is an I/O-boundary failure on a best-effort observation channel, not our data. The response is already over, the closing send is never retried by design, subscriber capacity follows the outstanding task, and the durable state stays in SQL for the next GET. Logging and ending is the only correct action, because re-raising would only abort a response that is already ending.

**Fixed (lead ruling, `0cfa7d7a3`).** The outer bounded-I/O handler in `BoundedComposerStreamResponse.__call__` caught `AuditIntegrityError`. Before the headers it became a 503; after them it was a quiet close. Every AuditIntegrityError the stream can see comes from our own rows, so it now propagates as a crash, and the finally block still releases capacity. AsyncWorkerAdmissionTimeoutError left the tuple as a redundant TimeoutError subclass. No caller or test relied on the quiet close. Red-first test `test_audit_integrity_error_surfaces_instead_of_closing_quietly` (3 cases); control MI1 killed. The handler that remains is not flagged by the scan, before or after.

## Codec residue and message families (branch `codex/0.8.10-codec-residue`, base `d7ad18596`)
Worktree `.claude/worktrees/residue-20261025`; logs under its `.claude/lanes/residue/`. Pre-rebase tip pinned at `preserve/0.8.10-residue-pre-rebase` (`b90a41463`).

| Commit | What |
|---|---|
| `3d91561d3` | Unfreeze `CurrentMessageProduction.messages`; validate every attachment-history append through `extend_attachment_history` |
| `b296259db` | Measured owner-test budgets (`tests/helpers/continuation_budgets.py`) |
| `82dd2ff49` | Native message-family cases from a real paid publication |

**Root causes, one per failure signature.**
1. *"guarded/native message has no actual registered current producer"* (2 message-family failed-result ids) and *"cannot pickle 'mappingproxy' object"* (4 attachment-application ids). ebf73a853 froze `CurrentMessageProduction.messages`. `freeze_fields` swaps each registered message for a mappingproxy copy, so the identity lookup in `capture_message` misses, and `detach_prompt_guard_evidence`'s deepcopy fails on the copies. **Identity-carrier justification** (accepted): the field hands over the exact objects the producers registered, the same reason ebf73a853 itself gave for leaving `RestoredTurnEvidence.messages` and the advisor observation carriers unfrozen. The primary path already takes owned thawed copies for the provider call. Keyless multiset `d7ad18596` vs `3d91561d3`: one added finding, `continuation_prompt_producers.py: FG3: Frozen dataclass 'CurrentMessageProduction' has container fields ['messages'] but no __post_init__`, and none removed (`keyless-residue.log`). carve-partition has been told. Control MA1 killed.
2. *attachment_history "DID NOT RAISE ValueError"* — **release defect**. `append_attachment_inspections` and two sibling sites appended with `model_copy(update=...)`, which skips pydantic validators, so `require_history_scope` never refused a read from another session on append. Pure probe: `model_copy` accepted session "other" and the constructor refused it. Red-first per site:
   - the existing impostor test (`append_attachment_inspections`);
   - `test_retained_history_refuses_a_read_from_another_session` (`_retained_history`, which returned a history holding another session's read);
   - `test_history_extension_refuses_a_repeated_application`, for the application append. At that site the immediate `encode_attachment_history` already re-validated, so the bypass never escaped it; the test pins the funnel the site uses.

   Controls MB1 (funnel back to `model_copy`), MB2 and MB3 (each site back to `model_copy`) killed.
3. *The 6 native message-family ids* (red since the overlay landed): `save_family_cut` stored a native recipe from a controlled received completion and never committed the provider effect `read_committed_primary_response_on_connection` requires. A probe found zero effect rows in the session. Production is right. The fixture now runs the real primary path, controlling only the SDK and guard HTTP. The refusal cases pin their guard: a content change to a paid response changes its digest, so the provider-effect binding refuses it, and a recorder without the call fails the recorder join. **Depends on U-45's `303286be6`**. Without it, every real primary publication fails with "Prompt guard content partition is not an owned producer identity", so on this branch alone these ids fail under that signature. On a local integration (`codex/0.8.10-u45-fix` `c26167c4d` plus this branch), message families and provider history give 17 passed. Controls MF1–MF3 killed.
4. *lease_expired, "Composer operation id was already used for another request", "primary action allowance expired during actual input preparation"*. The cause is wall-clock budget against host speed, not xdist: the ids fail serially at -n 0 too. At load 35–60 a phase took up to 70 s against a 30 s non-heartbeated fixture lease, and input preparation took more than 5 s against a 5 s allowance. The "already used" message is the same lost lease, seen from `require_continuation_mutation`. Every expiry test forces expiry through SQL, so the values only need to be long enough. Sizing: `OWNER_LEASE_SECONDS = 600`, more than eight times the longest measured phase and the authority's claim-lease ceiling, with renewals using it too; `PRIMARY_ALLOWANCE_SECONDS = 120`, the policy fixture's provider physical bound. After the change, cold-restore, primary-result and p4-application-store run 72 passed. `test_continuation_attachment_application.py` is U-35b's file, so the same change went to design-runtime as a patch.

**Decode cost (production finding, for design-runtime and U-45 part 2).** Reproduce on `dcb742ecc` or later:
```
cd <worktree> && PYTHONPATH=<worktree>/src:<worktree>/elspeth-lints/src \
  .venv/bin/python -m cProfile -o p4.prof -m pytest -n 0 -q -p no:cacheprovider \
  "tests/unit/web/sessions/test_continuation_p4_application_store.py::test_two_actual_applications_restore_one_shared_graph_and_do_not_repeat_business_sql"
python -c "import pstats; pstats.Stats('p4.prof').sort_stats('cumtime').print_stats('elspeth/(web|contracts)', 30)"
```
Measured for one test with two applications, 52 s under cProfile:

| Function | Calls | Cumulative |
|---|---|---|
| `contracts/hashing.py:canonical_json` | 50,545 | 22.5 s |
| `coordination/composer_operation_authority.py:read_continuation_turn_evidence` | 3 | 20.0 s (6.6 s each) |
| `composer/continuation_evidence_application_data.py:require_evidence_application_links` | 28 | 18.9 s |
| `composer/continuation_prepared_p4.py:prepared_p4_members` | 670 | 17.2 s |
| `composer/continuation_application_graph.py:decode_application_graph` | 44 | 15.8 s |
| `contracts/credential_material.py:find_credential_material` | 46,159 | 5.2 s |

The repetition is visible in the callers. `require_evidence_application_links` decodes the same application graph 22 times inside one evidence read, through `decode_application_graph`, and `prepared_p4_members` re-decodes through `_joined` (216 calls) and `decode_prepared_p4` (291). Profile file: `.claude/lanes/residue/p4.prof` in the residue worktree.

## Gates re-derived in-commit
- Soft-mapping census re-pinned with `scripts/check_contracts.py --write-census` in `ba7562ea8` (2738→2740): the two outbound provider tool-definition dicts in `continuation_controls.py`, the same shape `recovery_controls.py` carries.

## U-43 (S7 check)
- Ran `test_patch_source_options_noop.py`, `test_source_rejection_contracts.py`, `auth/test_middleware.py`, `test_shareable_reviews_routes.py`, `test_composer_operation_authority.py`, `test_composer_continuation_claim_start.py`, and the shareable-review frontend tests on the tip: 497 + 50 passed, 4 failed.
- The 4 failures are `test_source_rejection_contracts.py::test_source_outer_fault_keeps_distinct_integrity_and_completed_outcome_accounting[*]` (`'_FakeLLMResponse' object has no attribute 'message'`). They fail identically on the target `cd90c8034` without my commits, so they predate this branch. Not fixed here (out of scope); flagged.
- Also red on the target, independent of this branch: `test_error_code_redaction.py::TestRegistryCensus::test_every_non_literal_code_site_is_a_reviewed_forwarder` (three unreviewed forwarders in `continuation_batch_data.py`, `continuation_p4.py`, `tools/generation.py`).

## Incidents
- `3322c037c` (now `214744da5` after rebase) carried a leftover mutation (a missing header returned 2), most likely from a mutation run interrupted when the session was stopped. The committed tests caught it; fixed in the next commit. Since then tests run immediately before every commit.
- A first U-42 cut put the version choice in `client.sendMessage`, which the UI path does not use (the store builds its own descriptor). Found by the A9-T test design; moved to `submitAndObserveComposerOperation`, the real single choke point.

## Playwright without uv
The Playwright backend `webServer` runs `uv run`, which must never run in a worktree whose `.venv` links to the main checkout. A shim named `uv` first on PATH (`.claude/worktrees/protocol-20261025/.claude/lanes/protocol/bin/uv`) accepts only `uv run python|elspeth ...` and runs them with the existing interpreter and this worktree's `src` on PYTHONPATH; it never creates or syncs an environment. Main venv confirmed intact after each run. Playwright suites were run one at a time per worktree, on ports 5291/8491.

## Blocked or owed
- Lifecycle-2 Stop and the D-2 replay hunk (runtime-owned `composer_operation_authority.py`); U-40 palette and driver seam (runtime U-37). The runtime branch `codex/0.8.10-runtime` has both as WIP.
- Rebase on landing: drop my duplicate `WebSettings.composer_continuation_enabled` and my startup refusal when the runtime's U-38 setting and policy land; keep only the `/api/system/status` field.
- PostgreSQL: P-5 written and run (`tests/testcontainer/web/test_composer_stream_replica_postgres.py`, 1 passed at -n 0; control MP5 killed; `p5.log`). The moved `composer_http_databases` fixture's existing consumer still passes (`xproc-fixture.log`). Owed after the runtime lands: P-7 D-2 concurrency, P-1–P-4b/P-6/P-8/P-9.
- At the landing rebase onto U-38: fold the planted-mutation fix (`30c562483` today) into its parent so no landed commit carries the mutation; pin the old tip at `preserve/0.8.10-protocol-u41-pre-rebase` first.

## COLD START (paused 2026-10-25)

**Units owned:** U-41 (C7 server), U-42 (C8 client), U-40 (C6 control, step 1 only), U-43 (S7 check), plus the codec-residue and message-family units and the AuditIntegrityError release defect.

### Branches and worktrees
| Branch @ tip | Worktree | Base | State |
|---|---|---|---|
| `codex/0.8.10-protocol` @ `0cfa7d7a3` | `.claude/worktrees/protocol-20261025` | `d7ad18596` (U-38) | **READY TO LAND** (lead checked: 17 commits, config.py byte-identical to the target). Pre-rebase tip pinned at `preserve/0.8.10-protocol-u41-pre-rebase` (`247b7f5af`). |
| `codex/0.8.10-codec-residue` @ `82dd2ff49` | `.claude/worktrees/residue-20261025` | `d7ad18596` | 3 commits; the per-commit landing check is still owed (see Next). Pre-rebase pinned at `preserve/0.8.10-residue-pre-rebase` (`b90a41463`). |
| `codex/0.8.10-residue-u45-integration` @ `1fb71a3bf` | `.claude/worktrees/protocol-basescan` | `codex/0.8.10-u45-fix` `c26167c4d` | Scratch WIP, **never land**: u45-fix plus the 3 residue commits as one commit, used only to verify the message-family rebuild. |

Scratch detached worktrees with nothing of value, removable with `scripts/worktree-cleanup.sh`: `protocol-landcheck` (detached at d7ad18596), `protocol-vcheck` (node_modules is a symlink to the protocol worktree's), `keyless-base-d7ad`.

### Done and verified (evidence under each worktree's `.claude/lanes/{protocol,residue}/`)
- **Protocol landing steps all done:**
  - fold of 44f56e823 into step 1 (`db005a63d`);
  - rebase onto d7ad18596;
  - duplicate field and startup refusal deleted;
  - per-commit landing rule: backend `landcheck/<sha>.xml` and frontend `landcheck/v-<sha>.xml`, 0 failures at every commit;
  - A9, W-1 and W-2 re-run (`a9-*-rb.log`, `pw-rb-*.log`);
  - keyless multiset d7ad18596 vs tip: 1 re-keyed finding (`keyless-ptip.log`).

  See "## Landing on d7ad18596".
- **AuditIntegrityError release defect** (`0cfa7d7a3`): the stream no longer turns our own-data integrity errors into a 503 or a quiet close. Red-first test, control MI1, 179 stream and route tests passed, landing check at 0cfa7d7a3 green (`landcheck/0cfa7d7a3.xml`).
- **CurrentMessageProduction unfreeze:**
  - identity-carrier justification accepted by the lead;
  - keyless +1 FG3, deliberate (`residue/keyless-residue.log`);
  - carve-partition informed.
- **Attachment-history validation defect:** red-first test per site, controls MB1–MB3.
- **Budgets** (`b296259db`): 72 passed in cold-restore, primary-result and p4-store.
- **Message families** (`82dd2ff49`): 17 passed on the integration (`residue/mf-real-final.log`), controls MF1–MF3.
- **Decode-cost profile:** in this report, sent to design-runtime.

### In progress, half-done
- **Residue landing check (rule a/b) not measured.** `residue/landcheck/run.sh` collected nothing (exit 5 at every commit) because its selection names `tests/unit/web/composer/test_continuation_prompt_producers.py`, which does not exist. Drop that entry and re-run. Under the new capacity rules: -n 3, `timeout 2400`, one pytest per lane.
- **Attachment-application lease patch** sent to design-runtime (U-35b owns the file): `residue/attachment-application-budgets.patch` (verified 9 to 2 failures) and `-full.patch` (also raises the two heartbeated adopt sites; its verification run was stopped for the pause).

### Next steps on resume
1. Land the protocol series when the coordinator says so. Nothing is owed on it.
2. Residue landing check, with the selection fixed:
   ```
   cd .claude/worktrees/residue-20261025 && .claude/lanes/residue/landcheck/run.sh d7ad18596 3d91561d3 b296259db 82dd2ff49
   ```
   Edit the script first: SEL minus the missing file, and `-n 3` under `timeout 2400`. Compare (id, signature) pairs as in the protocol landing check.
3. **303286be6 dependency:** `82dd2ff49`'s six native ids pass only with U-45 part 1 (`303286be6`, "keep prompt-guard provenance owned across the primary thaw"). Without it they fail with "Prompt guard content partition is not an owned producer identity", a new signature under existing ids, explained in the commit. Hand the residue branch over only after U-45 part 1 lands, then rebase onto that target and re-run the landing check. It should then be strictly decreasing.
4. Held for the lead's go: the pre-existing reds in `tests/unit/web/composer/test_continuation_prompt_producers.py` (U-44b queue), after U-45 lands. Reuse U-45's `thaw_guarded_messages` / `PROMPT_GUARD_PROVENANCE_KEYS` seam.
5. **U-38c line coordination with design-runtime:**
   - U-38c parses AdmittedTurnEvidence once and changes journal admission to take the decoded graph. It touches the continuation evidence, application and p4 files near my residue sites (`continuation_attachment_application.py` `extend_attachment_history` call, `continuation_attachment_result.py` `_retained_history`) and the tests that use my helpers (`continuation_budgets.py`, `prepared_runtime(tool_calls=)`, `native_case`, `install_changed_native_source`).
   - Agree the order with design-runtime before either side rebases. Whoever lands second re-applies, not reverts.
   - Also coordinate the attachment-application budget patch: design-runtime folds it into U-35b, and their file has a new release_then_recover/live_handover parametrisation at bedb06605.
6. After U-35b's v2 `request_cancel` lands: replace the interim v2 Stop refusal with the real lifecycle-2 Stop (part of U-41, owed before the 0.8.10 merge), then run PostgreSQL ids P-7, P-1–P-4b, P-6, P-8 and P-9.
7. After U-37: U-40 wiring (palette call sites, driver seam, handoff-lineage reader, Advisor follow-through), then one Astra review.

### Open questions and rulings owed
- **design-runtime:** fold the attachment-application budgets into U-35b, and settle U-38c ordering against the residue branch.
- **Coordinator:** when U-45 part 1 lands, so the residue branch can rebase and hand over.

### Hazards
- A pytest selection with a missing path can collect nothing and still look like a run (exit 5). Read exit codes, not just "passed" lines.
- Host load reached 60–78 this session. Timing-bound continuation tests failed by speed alone before the budget commit, and attachment-application ids still do until the patch is folded. Do not read those as regressions.
- KNOWN HANG: directory runs must `--ignore` `tests/unit/architecture/test_web_landscape_mutation_fencing.py` and `test_web_reserve_reviewed_contracts.py`.
- Never run `uv` in these worktrees: `.venv` is a symlink to the main venv. Playwright goes through `.claude/lanes/protocol/bin/uv` (shim), ports 5291/8491.
- `keyless-base.log`/`.norm` in the protocol lane are from an older tree. Use `keyless-d7ad.log` as the base.
