# R3 remediation pause handover

Snapshot: 2026-08-03T19:38:00+10:00 (Australia/Canberra)

Use this brief with
[`2026-08-03-r3-rca-remediation-tracker.md`](2026-08-03-r3-rca-remediation-tracker.md).
Live Filigree and the release branch remain authoritative if they have moved
since this snapshot.

## Safe stop state

- Release checkout: `/home/john/elspeth`, branch `release/0.7.2`, HEAD
  `d3fb24c890f42a37b8ae34c9472461010c5631bd`, 68 commits ahead of
  `origin/release/0.7.2`.
- Every fix classified as completed and independently reviewed in this wave is
  committed on the release branch. There is no cherry-pick, merge, rebase, or
  conflicted-index operation in progress.
- The release checkout's only working-tree changes are pre-existing installer
  work owned by other agents:
  `installer-control-plane-policy.json.tftpl`,
  `installer-regional-resources-policy.json.tftpl`, and untracked
  `.acceptance-r3/`. Preserve them.
- Loomweave refresh `a86ff356-bc5d-4b0e-8f54-5ce99f5204a1` completed at
  exact release HEAD `d3fb24c89`; the index is fresh with 71,034 entities and
  137,904 edges.
- No AWS resource or database mutation occurred during this final checkpoint.
  No subagent held AWS authority.
- The combined full backend/frontend gate has intentionally not run after the
  latest integration wave. Localized evidence is recorded below and in the
  companion tracker.

## Completed fixes durable on `release/0.7.2`

The complete branch inventory is available with:

```bash
git log --oneline --reverse origin/release/0.7.2..release/0.7.2
```

The demo-aware completed integrations are:

| Area | Release commits | State at pause |
|---|---|---|
| Advisor projection, injection evidence, and re-review state | `ce61162bf` through `e4a8a5638`, plus `656488a01` and `4290b82cc` | D1/D2 current-head audits pass; successful re-review D3 leakage remains isolated below |
| Invented gate plugin/decision heading and stated gate routing | `313f1c6fe`, `ffdd39da8`, `23040b5b4`, `26a610340` | Topology intent and clarification precedence are durable; live acceptance remains |
| Textract deployment-region and S3 preflight proof | `ab9ce7c9a`, `89eeeaf54`, `b37be31a9` | Core/runtime fix durable; live AWS proof remains |
| Execution source proof and gate topology | `0104a0255`, `a0b112484`, `ca69a5653`, `ca16fe0f0` | Web validation/execution blocks the numeric string gate; guided persistence follow-up is isolated below |
| Readiness and action admission | `1f6677371`, `e28f08aef`, `d5c50dd5b`, `e326dab0c`, `127267811`, `3b173a4e0`, `9016ce6a6` | Backend/frontend parity integrated; 148 exact release tests passed for the later action slice |
| CSV audit characteristics and maintained corpus identity | `d78fbbed6`, `10179f2c1` | Integrated and corpus regression verified |
| Run diagnostics UI | `9130e1209`, `0928c6ac6` | Integrated; 26 release-checkout drawer tests passed |
| Compose authoring aids and field preservation | `2906409e1`, `e1d31f104` | Integrated; 176 focused tests passed |
| Guided unchanged-node custody | `b424c08c4` | Integrated and independently reviewed |
| HTTP request correlation | `8101f1f08`, `1537c7885`, `39c6b14a7` | Integrated; 32 exact release-handler tests passed |
| Guided chat revision custody | `7afa62b6d`, `c4cade0f0` | Integrated; independent review clean and 10 release tests passed |
| Gate per-row `on_error` policy | `a80ea0259`, `947c242ac`, `65acd35fb`, `e3804416f` | Integrated; 72 backend and 56 frontend release tests passed |
| Guided terminal progress | `5dfbe873f`, `b65782e3a`, `8486568af`, `859c2a642` | Integrated; independent review clean, 87 progress plus 10 chat-custody tests passed |
| Operator-profiled S3 source | `e7f6f1521`, `d6d980b89`, `8ddf317df`, `91bad304e`, `131a5f584` | Integrated; dual security review clean and 629 release tests passed; live AWS read remains |
| Human-readable coordination state | `278593c09`, `d3fb24c89` | Tracker is current through the final review holds |

## Isolated work that is not release-ready

### Required controls on the freeform compose loop

- Filigree: `elspeth-981130d70a`, `fixing`, assignee
  `codex-compose-required-controls`.
- Worktree: `/home/john/elspeth/.claude/worktrees/compose-required-controls`;
  branch `codex/fix-compose-required-controls`; HEAD
  `b2a187da0bbbb9fd2cee37ff39741cf9db99fecf`.
- All nine candidate files are staged and none are unstaged. Do not reset or
  restage unrelated files.
- Exact named and multiple blob-backed explicit proposals now preserve source
  names/blob references, insert controls before consent, retain truthful
  `set_output` audit identity, and omit blob identifiers/storage paths from
  review/audit projections.
- Evidence: 24/24 focused tests and 231/231 localized
  proposal/audit/blob/lifecycle tests passed; Ruff, formatting, mypy, secret
  scan, and relevant composer/session hooks passed.
- Commit is deliberately absent. `check-contracts` rejected four new
  `dict[str, Any]` return annotations in `pipeline_proposal.py`:
  `_project_owned_composition_state`,
  `owned_composition_state_authority`,
  `owned_composition_state_execution_arguments`, and
  `owned_composition_state_review_arguments`.
- Resume by replacing only those four broad return shapes with narrow owned
  `TypedDict` contracts, rerunning the 24 + 231 slices and commit hooks, then
  obtaining a fresh independent review. The eventual release cherry-pick must
  include `2f68a281f`, `b2a187da0`, and the follow-up commit in order.

### Guided gate-proof persistence and blob custody

- Filigree: `elspeth-fd32c3e6fd`, `fixing`, assignee
  `codex-gate-proof-guided-validation`.
- Worktree: `/home/john/elspeth/.claude/worktrees/guided-gate-proof-validation`;
  branch `codex/fix-guided-gate-proof-validation`; clean commits
  `b4fb70270` then `7fee879dc`.
- The follow-up uses nominal `GuidedReviewedBlobBinding` custody, rejects
  mixed/private/conflicting carriers, and distinguishes legitimate no-claim
  proof abstention from a claimed sentinel whose session/path/status custody
  fails. The latter is hard-invalid with zero prefix reads.
- Real ExecutionService tests cover observed `250.00`,
  `row['amount'] > 500`, and discard error policy: authenticated custody reads
  one verified prefix and fails check 25; wrong session/path/status/not-found
  all fail closed. Public `_state_response` and persisted/replay
  `composer_meta` projections are covered, including plural sources.
- Evidence before final re-review: 51 focused tests passed; Ruff, formatting,
  targeted mypy, diff-check, and commit hooks passed. Full pytest and Wardline
  were intentionally not run after the pause request.
- Final read-only re-review returned **Changes requested**. Sixteen exact tests
  passed for all previously reported custody/redaction cases, and the chain
  applies cleanly to `d3fb24c89`, but one residual P1 lifecycle defect remains:
  `_authoritative_proof_blob_resolver` collects sentinel IDs from a guided
  session whose terminal kind is `EXITED_TO_FREEFORM`. The same uninspectable
  explicit freeform source is green without guided history and becomes
  `source_inspection_failed` solely when exited historical state contains its
  UUID. Stale audit history must not change current freeform semantics.
- Resume at the narrow existing seam: skip sentinel-claim collection for
  `EXITED_TO_FREEFORM`, matching
  `reattach_guided_blob_refs_for_public_export`. Re-run the 16 reviewer cases,
  the candidate's 51 focused tests, static gates, Wardline, and re-review. Do
  not cherry-pick either commit yet.

### Advisor successful-repair surface and compose deadline

- Filigree: `elspeth-ca751fa4e1` (`fixing`) and
  `elspeth-57232f6f3c` (`in_progress`), both assigned to
  `codex-advisor-surface-deadline`.
- Worktree: `/home/john/elspeth/.claude/worktrees/advisor-surface-deadline`;
  branch `codex/fix-advisor-surface-deadline`; base `278593c09`; no candidate
  commit.
- Dirty files only:
  `src/elspeth/web/composer/no_tool_policy.py`,
  `src/elspeth/web/composer/service.py`,
  `tests/unit/web/composer/test_advisor_checkpoint.py`, and
  `tests/unit/web/composer/test_service.py`. `tool_batch.py` is untouched.
- Six new real D3/deadline regressions pass. The broader advisor slice reached
  170 passed and two stale wording assertions; those assertions were corrected
  but not rerun after the pause request. Wardline passed before the final
  test-only edits. `git diff --check` passes. Ruff/mypy/lints and independent
  review remain.
- Resume by rerunning the two corrected tests and focused advisor slice, then
  Ruff, mypy, composer lints, Wardline, and independent review before commit.
- The truthful tool-timeout counters/`failed_turn` change remains a separate
  small follow-up. Do not touch `tool_batch.py` until the required-controls
  lane above is committed and releases custody.

## Tracker and environment custody

- Human tracker:
  `docs/acceptance/2026-08-03-r3-rca-remediation-tracker.md`, source branch
  `codex/r3-rca-remediation-tracker`, latest source commit `b3b8d4cee`,
  integrated on release as `d3fb24c89`.
- Coordination task: `elspeth-f944ec61f7`, `in_progress`, assignee
  `codex-r3-rca-coordinator`; latest coordination comment is `2213`.
- Latest focused hold comments: guided proof `2214`; required controls `2215`.
- Deployed AWS candidate was still the older `4baf1109` image at the last
  read-only inventory. The release integrations above therefore need a deploy
  agent before live acceptance is meaningful.
- The earlier root-owned AWS work in the tracker was diagnostic/read-only:
  ECS/CloudWatch inventory and a PostgreSQL transaction explicitly set read
  only. Do not infer deployed acceptance from local tests.
- `docs-archive/acceptance/` remains ignored. Deferred task
  `elspeth-01c627b420` records that 89 acceptance-related issues cite evidence
  absent from a fresh clone. Keep this behind demo-aware core work.

## Resume order

1. Repair the guided `EXITED_TO_FREEFORM` authority leak at the existing
   terminal-kind seam, rerun the 16 + 51 focused slices and static/Wardline
   gates, then obtain a clean re-review before cherry-picking both existing
   commits plus the follow-up and advancing the bug to verifying.
2. Replace the four broad required-controls return shapes, rerun localized
   gates, obtain independent review, and cherry-pick the complete three-commit
   chain. This releases `tool_batch.py` custody.
3. Reverify and commit the advisor D3/deadline work; review and integrate it.
   Then make the separate truthful tool-timeout counter/`failed_turn` commit.
4. Refresh the text tracker and Loomweave after those product integrations.
5. Start selected-node/wire rewind `elspeth-dca1e81c58` and registry
   write-boundary validation `elspeth-7bd0141bbe` only after their shared-file
   owners release custody.
6. Run the combined full backend/frontend/lint/Wardline/Warpline gates once
   the core batch stabilizes.
7. Hand the exact release SHA to the deploy agent, then run one root-owned live
   demo acceptance covering advisor repair/CLEAN, guided numeric-gate refusal,
   mixed good/bad gate routing, profiled S3, Textract region proof, request-ID
   CloudWatch correlation, and diagnostics UI evidence.
8. Resume installer-only IAM/Terraform/runbook/shell work last. Textract, S3,
   validation, runtime, and audit behavior remain core/demo-aware work.

## Do not do on resume

- Do not discard or overwrite the two installer policy edits or
  `.acceptance-r3/` in the release checkout.
- Do not cherry-pick the staged required-controls state without the
  `check-contracts` correction and fresh review.
- Do not integrate the dirty advisor state without rerunning the corrected
  expectations and repository gates.
- Do not treat a claimed blob whose custody cannot be authenticated as
  ordinary proof abstention.
- Do not run judge-signature signing or acquire the operator HMAC key during
  ordinary feature work.
- Do not mutate AWS from a subagent. Root remains responsible for live
  mutation deconfliction and the operation ledger.
