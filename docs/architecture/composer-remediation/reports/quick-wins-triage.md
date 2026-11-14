# Composer remediation: quick-wins triage

Date: 10 November 2026. Read-only triage of branch `fix/composer-tool-remediation` (tip `a1cc53b6d`, implementation checkpoint `dd3b3524c`). No code, tests or existing docs were edited, no test suites were run, and nothing was fetched. Every "verified in code" statement below means I read the current source at this tip; it does not mean I ran the failing scenario. Where I rely on the audit's own demonstration I say so. Source-line numbers are at this tip.

Limits of this review:

- I did not run any test, probe or reproduction. "Verified" = the defective code path is present as the audit describes.
- The local `main` / `origin/main` refs in this worktree are stale (both resolve to `3b28d537b`), so I could not see PR #354 or the two reported conflicts. Section 2 takes them from the dispatcher.
- I did not read all 46 tool sheets end to end. I read the register (README), remediation plan (Q1-Q30), the preview, set_metadata, request_interpretation_review and diff sheets, and checked the shortlisted items in source.
- Likelihood is mostly my judgement from code routing, not telemetry. It is marked "guess" where so.

## 0. Bottom line

1. The integrated work (D1 approval lifecycle, identity, cache audit, lifecycle, queue fields) covers most of what the audit called the worst: 02.03/Q1, 02.10-02.12, 03.01-03.05, 08.01, 11.15, MCP preview. That is a status taken from the checklist and reports: integrated, NOT behaviour-verified at the tip (my own spot checks were limited to the canonical blob-id use in `tools/blobs.py`, the path guards in `tools/_common.py` and the absence of a queue-field drop). The remaining 300-odd open boxes are mostly guidance wording, presentation, rare-tool hardening and design decisions.
2. Verification of the integrated work should be handed to CI (`.github/workflows/ci.yaml` already runs the ordinary shards, the Landscape proofs with a native aggregator, the PostgreSQL testcontainer job and frontend unit tests). The planned local ceremony duplicates it. See section 2.
3. Only about six open defects are worth doing now. Two are real turn-losers on ordinary paths (JSON preview: small; opt-out interpretation review: biggest but NOT small, audit rates its implementation risk High), the rest are small and mechanical. The hot-path authoring tools (`set_output`, `patch_*`, `upsert_node`) got only a partial pass: see QW9.
4. Do not build blob pagination. Do not do the full 11.19 test matrix. See section 3.
5. One structural fact the audit under-weights: every fresh session's first turn goes through the planner, not the freeform loop (`src/elspeth/web/composer/service.py:1234-1237`, `planner_eligible = state_is_empty and ...`). Planner-only defects are therefore on the hot path of every new session. I could not measure how often they bite (section 4).

## 1. Top quick wins (ranked)

Ranking is (impact x likelihood) / effort. Effort: S <= 1h, M half-day, L day+. "Gate" = whole-tree gate or PostgreSQL need.

### QW1. Opted-out session loses the whole turn when the model calls `request_interpretation_review` (10.01, XC-17) - highest impact, NOT a quick win

- Defect: in an opted-out session the writer auto-resolves the review and appends a new composition state inside the tool call, but the tool returns `updated_state=state` (the pre-call snapshot). Turn persistence then compares against the pre-batch id and raises `StaleComposeStateError`, rendered as 409. Audit probe observed zero chat rows saved, head at v2.
- Verified in code: the producing half only. `src/elspeth/web/composer/tools/sessions.py:3408-3418` returns `updated_state=state` (and `affected_nodes=()`) in the `AUTO_INTERPRETED_OPT_OUT` branch. Not verified: the persistence half (`src/elspeth/web/sessions/service.py:2045` per the audit; `src/elspeth/web/composer/turn_audit.py:504`); I rely on the audit's run.
- Reachable by ordinary users: the opt-out is a user-facing feature (`/interpretations/opt_out` endpoint in `src/elspeth/web/sessions/schemas.py`, opt-out indicator in `NarrativeResults.tsx`). The tool is advertised on every freeform turn regardless of opt-out (audit sheet row, `provider_gateway.py:391`), and the completion repair gate injects a `[composer-system]` message telling the model to call it for missing-event sites (`composition_completion.py:1591`, audit sheet). How many sessions are opted out: unmeasured.
- The audit's own position (sheet section 6 and 6.1 item 6): making the result truthful is not enough; the effect receipt must carry the new head AND the compose loop must adopt it, and it labels changes here "High" implementation risk. So the real fix is L-ish and touches turn persistence/state fencing.
- Fix options: (a) full fix as the audit describes (return and adopt the appended head); (b) cheaper containment I have NOT verified: do not advertise the tool (and do not inject the repair-gate instruction) when the session is opted out, since the backend's finalization surfacer already writes the same events. (b) is a palette/guidance change and may carry strict-profile and skill/hash pin churn; check `provider_gateway.py:391` and the completion gate before choosing.
- Minimal test: one scripted compose turn through the real compose loop in an opted-out session asserting the turn completes, chat rows are written, head is v2 and the requirement is resolved; negative control: non-opted-out session unchanged.
- Effort: (b) M, (a) L. Risk: (a) high. Gate: if session turn persistence is touched, AGENTS requires the PostgreSQL testcontainer selection (`pytest tests/ -m testcontainer -n 0`); I earlier wrote "none" and that was wrong.

### QW2. `preview_pipeline` ends the turn with `FrameworkBugError` for JSON sources (12.01, Q6, preview_pipeline-N1)

- Defect: three JSON-source proof diagnostics fail the strict `PreviewResponse` admission. The universal-loss and sampled branches have no `"source": "blob"` and carry undeclared keys (`selected_record_count`, `sample_extra_record_count`, `all_rows_inspected`); the sampled one also uses `severity: "warning"` which the contract does not allow. The configured-JSON `source_inspection_failed` branch also lacks `"source"`.
- Verified in code: yes. `src/elspeth/web/composer/tools/generation.py:4041-4070` (universal and sampled dicts), `:3889` (inspection-failed evidence), against `src/elspeth/web/composer/tools/_generation_responses.py:192` (`ProofEvidence`: `source` required, `extra="forbid"` via `_ResponseRecord`) and `:243-245` (`ProofDiagnostic.severity` is `Literal["blocking","info"]`). Audit demonstrated the first two via real custody and a freeform run; I did not run them.
- Who/how often: `preview_pipeline` is the validation step the skill tells the model to finish on, so it is on the hot path of every authoring session; the defect needs a blob-backed JSON source with `schema.mode: fixed`, `on_validation_failure: discard` and records carrying undeclared fields. JSON sources are less common than CSV: moderate likelihood (guess). When it triggers the user loses the turn to a framework fault.
- Fix (producer-only, no contract or wire-fixture change): add `"source": "blob"` to the three evidence locators; drop the three undeclared count keys (or fold into the message text); change the sampled diagnostic's severity to `"info"` (matches audit E1 and keeps `_BLOCKING_DIAGNOSTIC_CODES` untouched).
- Minimal test: two real-custody preview cases from the audit (`[{"a":"x","b":"y"}]` with fixed `a: str` + discard; `[{"a":"x","b":"y"},{"a":"z"}]`) passing `admit_discovery_result`; negative control: revert one `"source"` and the test must go red.
- Effort: S. Risk: low. Gate: none if the response contract is untouched (avoid the `generation_response_wire_*.json` fixture and the skill-vocabulary pin by not adding codes).

### QW3. `get_blob_content` reports "file too large" as a credential finding (04.10, Q5, XC-14)

- Defect: any decoded file over 262,144 UTF-8 bytes is refused as `credential_material_rejected` with "store the value through the secret service", indistinguishable from a real credential.
- Verified in code: the path is as the audit describes. `src/elspeth/web/composer/tools/blobs.py:2160-2200` builds the full-content payload and runs `_credential_safe_blob_discovery_result` (`:117-133`), which maps every `CredentialMaterialRefused` to `credential_material_rejected`. `read_attachment` pre-checks size honestly (`:2313-2316`). I did not re-run the +/-1 byte control.
- Who/how often: a user with a CSV over 256 KiB whose model reads it with `get_blob_content` instead of `inspect_source`. Real but the tool is a secondary one (guess). Harm is a misleading dead-end plus bad advice, not data loss.
- Fix: before the credential scan compare `size_bytes` (or `len(data)`) with the limit the guard actually applies and return an honest size refusal. The constant `read_attachment` uses is `READ_ATTACHMENT_MAX_UTF8_BYTES = min(JSON_MAX_TOTAL_UTF8_BYTES, DEFAULT_CREDENTIAL_TRAVERSAL_POLICY.max_string_bytes)` (`src/elspeth/web/composer/redaction.py:3411`); the 262,144 figure is the audit's and I did not confirm it equals that constant or the traversal policy's `max_string_bytes`. Confirm before reusing it, because the point of the fix is that the two limits cannot drift.
- Test: file at limit and limit+1 (plus a credential-positive control still refused as credential). Effort: S. Risk: low. Gate: new error code needs registering in `src/elspeth/web/composer/error_codes.py` and may touch pinned vocab tests; reuse an existing code if one fits.

### QW4. `remove_output` leaves edges pointing at the deleted sink (08.10, Q12, XC-16)

- Defect: `without_output` deletes the output only.
- Verified in code: yes. `src/elspeth/web/composer/state.py:8290-8295`, and the handler `_execute_remove_output` (`src/elspeth/web/composer/tools/outputs.py:235-249`) calls `without_output` and returns `_mutation_result` with no edge reconciliation; `outputs.py` has no commit since the audit baseline except `1bc91710f` (MCP paths). (Contrast `without_node`, `:8250-8257`, which does drop edges.)
- Who/how often: anyone restructuring a pipeline by removing a sink; the model sees high-severity dangling-edge errors and must clean up with `remove_edge`. Self-healing but wastes a round and the graph view may show phantom routes. Moderate frequency (guess), low severity.
- Fix: drop edges whose `to_node` is the removed name in `without_output`. Do not touch the "source" literal / name-collision cases (08.07-08.13) in the same change; those are the larger endpoint-reconciliation design.
- Test: remove a sink that has an incoming edge, assert edges gone and version advanced once; control: edge to another sink retained. Effort: S. Risk: low-medium (state.py is core; check that `reconcile`/mirror edges logic downstream does not assume the edge exists). Gate: state.py is widely scanned; run the state unit tests plus whole-tree gates for state.

### QW5 (demoted to defer). Planner closes the blob-inventory gap on a truncated list; nondeterministic tie order (03.12, 03.09, Q13-lite, XC-13)

- Verified in code: the defect exists. `src/elspeth/web/composer/pipeline_planner.py:439-442` (unconditional key), `:535-555` (`_resolved_discovery_information_keys` is completeness-aware only for `list_models`); `src/elspeth/web/composer/tools/blobs.py:540` and `:568-569` (`order_by(created_at.desc()).limit(50)`), `src/elspeth/web/blobs/service.py:2878`.
- Why demoted: both queries are filtered by `session_id`, so it takes 51+ blobs in ONE session (guess: not ordinary use). And the containment (leave the key open when the window is full) has an unchecked side effect: I did not establish what an unclosed key does to the planner loop (`pipeline_planner.py:5158-5160` gates only the "all gaps closed, emit now" notice on `pending_information`; manifest narrowing happens at `:5155`). If an open gap leaves the planner re-listing until `DISCOVERY_EXHAUSTED`/`DISCOVERY_NO_GAIN`, the change turns a silent truncation into a dead end, and a session with exactly 50 blobs is a false positive. It also changes provider-call counts on the rootless first-turn path, which the AGENTS standing review trigger watches. Do it only after reading that loop; otherwise defer with pagination.
- If done: id tie-break in the three queries is safe and independent (S). Do not change the `list_blobs` bare-list wire shape.

### QW6. Planner restore/handoff receipts classified `unknown_tool` (11.19)

- Defect: planner received-response capture does not include the recovery tool names in `known_tools`.
- Verified in code: yes. `src/elspeth/web/composer/pipeline_planner.py:3921` builds `known_tools` from the wire registry plus the terminal name only; `PLANNER_RECOVERY_TOOL_NAMES` is imported at `:82` and used at `:1242`, `:1897`, `:4379` but not here.
- Impact: private audit-evidence category only; no authority or user-visible effect (per `recovery-audit-triage.md`, and consistent with what I read). Frequency: only when the planner uses a recovery control.
- Fix: union `PLANNER_RECOVERY_TOOL_NAMES` into that set. One line.
- Test: ONE persisted planner restore (or handoff) receipt asserting the category equals the recovery tool name, plus the removal mutant going red. Skip the seven-oracle matrix in the triage note.
- Effort: S. Risk: very low. Gate: none expected.

### QW7. Freeform `diff_pipeline` always refuses; planner `diff_pipeline` always reports zero changes (02.04, 02.06, GS-01), plus guidance that tells the model to call it (Q-text)

- Defect: freeform never passes a baseline to `execute_tool` (no `baseline` in `tool_batch.py`; `tools/_dispatch.py:811` defaults to `None`), so the tool returns `diff_baseline_unavailable`. Planner passes `baseline=current_state` (`pipeline_planner.py:3462`), so it diffs a state against itself. `skills/pipeline_capabilities.md:417` ("then preview and diff it") and `skills/pipeline_composer.md:341` invite the call.
- Verified in code: both baseline facts yes. Not run. Audit also shows each failed freeform diff later appears in a mutation's `resolved_errors` (XC-20); I verified the mechanism line exists (`tool_batch.py:3419` assigns `last_validation` from every result) but not the consequence.
- Impact: wasted provider round per call and misleading deltas; no data loss. Frequency depends on how often the model follows the skill's "diff it" advice: guess, moderate.
- Fix without waiting on D8: remove the "preview and diff it" instruction (skill text) so the model is not steered into a tool that cannot work. Anything else (supply a turn-start baseline, remove the planner tool, redefine as candidate vs current) is a decision (D8) with planner tool/prompt-hash pin churn; defer.
- Test: existing skill-text parity tests updated; no new behavior test. Effort: S. Risk: low. Gate: skill hash pin churn (update the pinned hashes deliberately, mechanical).

### QW8. Planner/skill palette mismatch (XC-01, 13.01-13.06) - MEASURE FIRST, then decide

- Claim (audit, scripted planner only): planner receives the freeform skill text that tells it to call tools it lacks; a mutation tool call ends planning with `DISCOVERY_ONLY` (`src/elspeth/web/composer/pipeline_planner.py:4859-4877`; this guard is verified).
- Why it could matter: planner = first turn of every fresh session (above). Why I did not rank it: it is not shown with a live model, `planner_authoring_aids.py` already scopes some call instructions (comment at `:947`), and tutorial-level composes worked on 0.8.9 per project memory.
- Action: before spending any effort, count `DISCOVERY_ONLY` planner outcomes and planner attempt rows by code in the dev deployment's composer audit data (I did not locate the table names; ask the audit/session schema owner). If the rate is non-trivial, fix the specific tool names the planner actually emits; if near zero, close 13.01-13.06 as "not worth it".
- Effort: S to measure.

### QW9. Hot-path authoring tools: checked only partially, listed as unchecked/possible S items

- 08.04 / Q11 second half (gate `on_success` accepted by `upsert_node`, validity reported true, runtime then fails "No producer for connection"): I grepped `src/elspeth/web/composer/tools/transforms.py` and found no refusal of a gate `on_success` (only the queue fields were preserved by `ec079378e`). Probably still present; not confirmed by running. Runtime preview/validate catches it before execution, so the harm is a wasted round and a false "valid", not a bad run. Fix S: refuse gate `on_success` (and transform `fork_to`) at admission with a coded refusal; test: gate with `on_success="side"` refused, `routes` form accepted.
- Q21 / 13.10 (skill/description says "full replacement options object" for merge-patch tools): the skill row is still present (`src/elspeth/web/composer/skills/pipeline_composer.md:1421`). Text-only S; skill-hash pin churn. Misleads read-modify-write patches.
- Q22 / 09.16-09.17 (false JSON `on_write_failure` advice on `set_output`/`patch_output_options`): UNCHECKED. `tools/_common.py:1872`, `:1937` and `tools/outputs.py:98-149` are the places to look.

### Candidates I looked at and dropped from the quick-win list

- 09.01/09.02 (non-string or NUL source/sink path crashes the turn, Q2): ALREADY FIXED in code. `is_local_path_value` guards in `src/elspeth/web/composer/tools/_common.py:2357`, `:2402` and the provider_config branch at `:2454-2460` (arrived in `1bc91710f`). The checklist still shows them open. Close after confirming a regression test exists (one `grep`/collect), do not re-implement.
- Q24 / 06.20 (openrouter ids): the main case (`provider` = `openrouter`, `openrouter/`) is routed through the catalog accessor already (`src/elspeth/web/composer/tools/generation.py:2590-2596`). Residual `openrouter/anthropic/` prefix form is an edge; skip.
- Q9 / 09.12 (splice refusal zero entries): partially addressed; `_splice_validation_rejection` (`src/elspeth/web/composer/tools/transforms.py:1479-1516`) forwards new entries and appends standing ones. Residual is wording only; skip.
- Q16/Q17 (ESTALE, `StorageAccountingUnavailableError` in composer tools): still unhandled in composer (only `sessions/routes/sessions.py` handles the latter) but need NFS revocation or an accounting-store outage; rare; S each if you ever want them.
- Q4 (`update_blob` on pending/error blob crashes turn): not verified in code; needs a listed pending/error blob; rare.
- 09.25/METADATA-N2 (imported YAML sink `interpretation_requirements` wedges later edits): reachable but needs an unusual YAML; `remove_output` clears it; defer.

## 2. Already-finished-but-unverified work: cheapest honest verification

What is pending (RESUME.md): lint-delta adjudication, broad Astra review, ordinary suite + 3 official Landscape proofs (up to 7,200 s each) + PostgreSQL, plus a not-yet-built local checker for shard/proof manifests. Static stages already passed at `dd3b3524c` (ruff 3 s, mypy 33 s, contracts 36 s).

Recommendation:

1. Merge main first, then verify. Verifying `dd3b3524c` now and then merging PR #354's changes invalidates the result anyway. Resolve the two test-file conflicts by hand in `tests/integration/web/composer/test_composer_against_gateway.py` and `tests/unit/web/composer/test_endpoint_affordance.py`; I could not see them (stale refs), so I cannot say whether they are text or semantic conflicts. Run `scripts/branch-safety-check.sh --intent merge` (and `--intent push` before pushing) rather than improvised git checks, and re-run static stages with `scripts/full-suite-gate.sh --execute --detach --stages ruff,mypy,contracts` on the merged tree (about 75 s of stage time at the last measurement; the script defaults to ruff,pytest only, so always pass `--stages`).
2. Use CI as the gate instead of the local gate. Read from `.github/workflows/ci.yaml`: it triggers on `pull_request` (and push) to `main`, `master`, `RC*` and `release/**` (lines 14-17), so a branch push alone does nothing; open a PR into main. The heavy jobs (`test`, `landscape-proof`, `testcontainer`) carry `if: github.event_name != 'pull_request' || head repo == this repo`, so the PR must come from a branch in the same repository (this branch is on `origin`, so it qualifies; a fork would silently skip them). `cancel-in-progress` is true for non-`release/` refs (lines 24-26), so a second push cancels the first run: push once, after the merge. The workflow then runs: static analysis, the ordinary test shards (`test`, line 370), `landscape-proof` (line 583; the same three proofs, with the `coverage` job at line 588 aggregating shard plus proof evidence for 3.12 and 3.13), the PostgreSQL `testcontainer` job (line 806, 90-minute limit), `integration`, `frontend-unit`, `gateway` and others. That is the repository's own definition of "merge-ready"; the local runbook (`reports/combined-wave-gate-runbook.md` steps 2-5) reimplements it and its own step 5 says the CI aggregator refuses local identity, so a local checker could never be authoritative. Cost: wall-clock of CI on the self-hosted runners, no human time. Read the verdict per the repo's own rule (filter by commit, take the `CI` row), and cancel superseded runs after non-final pushes (the runners share a host).
3. Lint delta: do it mechanically, once. Diff the two keyless corpora as multisets of (path, rule, full message): baseline `b4064d270` has 5,433 findings, `dd3b3524c` has 5,454, a net +21 with unknown gross adds and removes. Only the tuples present in the new set and absent in the old set need judgement (fix or leave ready for adjudication per AGENTS "Scope" rules); line-number-only shifts need none. If the gross addition count is large, group by file and fix at the source rather than reviewing line by line. Do this after the main merge so the baseline is the merged tree, not `b4064d270`. CI does not run the signed trust-tier check, so this is the only lint evidence required; no signing, no Astra pass on it.
4. Cut or defer: (a) the broad Astra review of a 119-file diff; each of the 11 slices already had a per-slice review with mutation controls. If you want one cross-slice look, restrict it to the files touched by more than one slice (`src/elspeth/web/composer/tool_batch.py`, `src/elspeth/web/sessions/routes/composer/proposals.py`, `src/elspeth/web/sessions/service.py`, `src/elspeth/web/composer/pipeline_planner.py`) and run it once on the PR diff while CI runs; it should not gate the merge; (b) local ordinary-suite, local proofs and local PostgreSQL runs; (c) the frozen-tree/manifest checker and its five qualification controls; (d) native Windows/macOS qualification (not claimed, not available); (e) re-running historical failures from `14fef5e53`.
5. Diagnose only what CI turns red. Per AGENTS, the Landscape/process-death flake family (`e2e/recovery`, `integration/pipeline`, `unit/engine/orchestrator`) is known to be nondeterministic under parallelism: re-run a named failure with `-n 0` and compare against the same test on main before blaming the branch.
6. Honest residual risk to state at merge time: CI runs Linux only; the portable process supervisor (`8c35a2e2a`, `dc67bdd2f`) is exercised on Linux only. That is acceptable for this single-developer pre-release project provided the PR description says so.

## 3. Stop / defer list

Stop polishing (do not do now):

- Blob pagination (03.06-03.08, 03.10, 03.11, 03.17, plus `blob-pagination-plan.md`, `pagination-service-brief.md`): defer indefinitely. The five-task design (HMAC cursors with a 30-minute lifetime and HKDF binding, v2 page envelopes, planner contiguous-coverage tracking with a 256 cap, a UI traversal with A-to-B-to-A fences, PostgreSQL mutation proofs) answers a window of 50 rows that is per session (`blobs.py:540`, `:568`). A session needs 51+ blobs to hit it. Do not even do QW5's containment until the planner loop effect of an open gap is read (see QW5); if the file panel ever matters, raise or expose `limit` on the existing REST route (it already accepts 1..500). Revisit only if the dev deployment shows sessions at or over 50 blobs.
- Recovery-audit 11.19 matrix: do QW6 only (one line, one persisted-receipt test, one removal mutant). The seven-oracle plan (malformed pre-admission, post-admission mixed, hatch dialects, privacy) protects a private audit category with no authority or user effect.
- Group 15 (user-visible tool text and approval presentation, 15.01-15.14): wording and card copy. Exception worth keeping for later: 15.09/Q28 (approver cannot see that an `upsert_edge` moves a producer's `on_success`), only because approvals are a trust surface. Not now.
- Group 13 guidance wording except what QW7/QW8 pull in (13.01-13.17).
- Group 12 diagnostic-code polish except QW2 (12.02-12.17): catalog coverage, pointer suppression, code splitting. Q10/12.09 (pointer for uncatalogued codes) is the only one with a plausible loop effect, but it carries skill-hash pin churn; defer until measured.
- Group 6 plugin-catalog fidelity (06.01-06.19) and group 7 secret-validation semantics (07.x): design-heavy; no demonstrated user failure on the hot path.
- Group 10 (interpretation-review reconciliation 10.02-10.17) except QW1/10.01.
- Group 14 advisor/recovery (14.x) except if malformed recovery controls matter (Q18, 500 `operation_failed` for a malformed `handoff_to_user`; verified only via the audit, rare).
- Group 16 test-infrastructure items (16.05-16.14), except noting that 16.15 (provider calls per transition on rootless/tutorial paths) is required by AGENTS only if you touch that chokepoint; none of the above does.
- 04.x deep reader refactor (shared fenced snapshot read, streaming, ESTALE, size budgets), 05.x blob lifecycle design (05.06-05.10, 05.15), 08.02-08.18 graph-endpoint reconciliation beyond QW4, 02.13/02.16 proposal dependency semantics, 09.x patch/metadata naming: need rulings or large refactors; rare tools or rare triggers.
- XC-20 (11.14, discovery failures polluting the next mutation's `resolved_errors`): cosmetic-to-misleading; fixing QW7's guidance removes the main trigger.
- XC-09 (MCP skips discovery response admission): MCP is a local stdio operator route.
- Decisions D3-D38: leave undecided unless a quick win above needs one (only D8 is touched, and QW7 avoids it).

## 4. Anything actually on fire that the audit under-ranked

All verified in source unless marked.

1. The planner owns every fresh session's first turn (`src/elspeth/web/composer/service.py:1234-1237`). The audit's severity framing treats planner findings (XC-01, XC-13, XC-24, GS-01 planner half) as one route among three. By routing they are the most-exercised path. Unmeasured; see QW8.
2. QW1 (opt-out turn loss) is demonstrated by the audit as a skill-induced path, not an obscure one, and loses the user's whole transcript for the turn. It sits in group 10 at 10.01, near the bottom of a 17-item list, with no high-priority label in the checklist.
3. QW2 (preview framework fault on JSON sources) is rated as a quick win in the audit (Q6) but sits in group 12 as 12.01 with no priority.
4. Freeform discovery has no turn cap. `composer_max_discovery_turns` is read only by the planner (`src/elspeth/web/composer/planning_application.py:305`, `pipeline_planner.py:4879`; grep of `src/elspeth/web` shows no freeform consumer), and `config.py:1311` still sums it into "configured turns". A model that loops on discovery on a non-empty graph runs to the compose deadline (audit measured 39-43 scripted provider calls in 8 s; live behavior untested). This is a cost/latency exposure, not a correctness bug, and it needs ruling D7 (cap vs rename), so I list it for awareness only.
5. Checklist drift: at least 09.01/09.02 are already fixed in code but unchecked; Q24's main case is done; 01.02-01.04 (re-baseline the audit against current source) were never done as a step. Before scheduling anything from the remaining list, spend 30 minutes per candidate re-checking it against current source (as I did above); the audit predates 17 fix commits and several findings have moved.

## 5. Proposed order, next 1-2 working days

Day 1 morning (about 3 h, mostly mechanical):
1. Merge or rebase onto current main; resolve the two test-file conflicts; ruff + mypy + contracts on the merged tree.
2. Push and open the PR; let CI run (this is the verification). Check capacity first so you are not running another broad suite on the same host.
3. In parallel: mechanical lint-corpus multiset diff (section 2.3); run the planner-outcome measurement (QW8) against dev data.

Day 1 afternoon (one worktree off the merged tip, one branch, focused tests only):
4. QW2 (JSON preview), QW6 (11.19 one-liner), QW3 (size vs credential, after confirming the limit constant), QW4 (`without_output`). Each with its single test and one negative control. Run focused tests per change plus whole-tree gates for the files touched (generation/state), once at the end.
5. QW7's skill-text removal, with deliberate pin-hash updates, in the same branch.

Day 2:
6. QW1 (opt-out turn loss): read `tool_batch.py` state accumulation and `turn_audit.py:504` first; try containment (b) (do not advertise the tool when opted out); only attempt the full fix if (b) is unsound. This is the only item that can overrun; time-box it to half a day and, if it does not close, leave it as the one named open risk. If it touches turn persistence, run the PostgreSQL testcontainer selection.
7. QW9 items if time remains (gate `on_success` refusal; skill wording).
8. Re-check CI on the final commit (filter by commit, take the `CI` row); merge. Update the checklist: close 09.01/09.02 with evidence, mark the cut groups "deferred by triage" with this document as the disposition, and stop.

Not on the list: any further review rounds, mutation campaigns beyond the one negative control per fix, or new design documents.
