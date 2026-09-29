# Worktree and branch release coverage audit

Snapshot: 2026-09-30, release/0.8.1 at `a2f0281ff09cb5a3caa997660c3b4fe8306fc675`.

Read-only audit of **41 local branch refs and 38 registered worktrees**, including the primary checkout and two detached donor trees. The Git refs, worktree registrations, inspected dirty-file bytes and index entries stayed stable during the instrument run (`frozen=true`). The report records the starting state before the misc-components integration.

## Method and limits

Compared commit ancestry, `git cherry` patch equivalence, merge-base-to-branch changed paths, exact release blobs, reverse applicability of file patches against a private index, and donor working/index bytes. Instrument controls proved exact-match, deliberately absent mutation, reverse-present and reverse-negative cases. Reviewed every ambiguous changed file in the web/guided/local-donor cohorts, and historical release blobs and successor commits for preserved WIP branches.

A `git cherry +` result is unmatched commit patch identity, not proof the functionality is missing. A donor blob differing from release likewise does not prove missing work. Exact historical incorporation, successor code and test inspection distinguish incorporated/superseded files from surviving omissions. This is coverage evidence, not merge readiness or test clearance. Ignored build outputs, runtime data and historical lane logs are outside the code-coverage scope; none were removed.

## Valuable unlanded work

| Branch | Surviving work |
| --- | --- |
| `docs/s2-tranche-roadmap` | Roadmap with preserved owner rulings and hold. |
| `feat/source-resume-20260929` | Sealed finite-file resume, runtime/source/checkpoint/purge updates and tests. |
| `feat/web-auth-policy-20260929` | Origin-bound auth candidate, config, refusal gate, tests and confidentiality design; safe request headers already incorporated. |
| `feat/web-browser-foundation-20260929` | Browser boundary and offline replay contracts; two useful uncommitted negative tests. |
| `feat/web-json-extraction-20260929` | Bounded JSON-record extractor and tests. |
| `feat/web-pagination-20260929` | Bounded audited GET pagination, fixtures and tests. |
| `feat/web-response-policy-20260929` | Response decoding/content bounds and validation, runtime integration and tests. |
| `fix/composer-llm-contracts-20260921` | Partial core port exists; source-approval reconciliation, planner guidance and supporting tests remain absent. |
| `fix/ssrf-block-unspecified-ipv6` | Unspecified/special-purpose/IPv4-embedding address rejection. |
| `fix/supply-chain-npm-audit` | CI npm audit, lockfile repairs and security contact. |
| `recovery/deferred-platform-wip-broken` | Unfinished detached interpretation validation with owned DTO; broken checkpoint needs completion on current code. |

The Composer contract branch contains a specific production omission: donor `_execute_set_source_from_blob` reconciles authoritative reviews and handles reconciliation failure, whereas release directly returns `state.with_named_source`. The existing release reconciliation in `_execute_patch_source_options` does not cover this function. Its planner aid still says the node-level prompt template is `STILL required`; donor explains the effective user template per query and optional shared fallback.

The broken recovery checkpoint cannot be copied wholesale. Its useful `interpretation_validation.py` and `test_interpretation_validation_inputs.py` are absent, and the release still retains process-local catalog/profile service objects. A complete current-compatible refactor must preserve authority and profile lowering behavior.

## Additional uncommitted value

- Web remediation plan: six documents under `docs/plans/2026-09-23-web-review-remediation*` are absent from committed release. Five donor documents match the primary checkout's uncommitted copies; `guided.md` has stale tool wording and needs a current retirement disposition.
- Proposal-persistence donor `tests/unit/web/sessions/test_composer_proposals.py` contains a missing exact `pipeline_metadata` shape assertion; the production projection itself is present.
- Browser donor test file contains two additional negative tests: malformed Unicode URL denial journaling, and oversized/malformed replay manifest rejection before hashing.
- Primary checkout has other pre-existing uncommitted documentation. Its state is preserved; it is not credited as committed release content.

## Incorporated or superseded cohorts

| Cohort | Disposition |
| --- | --- |
| Guided acceptance/docs/frontend-workspace/frontend-source-tests/planner-tests/tutorial-review | Intended changes incorporated; later freeform retirement and harness refinements supersede old bytes. |
| Dirty Composer-core/frontend/neutral-ledger/proposal-persistence/sessions/periphery donor trees | Production changes incorporated or superseded; the one test-only projection assertion above remains. |
| Web request policy, Composer preflight, detail pipeline, browser-egress docs | Cherry-pick equivalents and current successors are present. |
| Structured extraction | Provenance outputs and deep freeze are present even though two commits have unmatched patch identities. |
| Modern web fetch and link normalization | Branch tips are ancestors of release; current committed work is incorporated. |
| Detached Azure diagnostics/profile-schema, K056 replay sink, prompt-edit donor | Current successors preserve inspected production intent. Pricing tests moved to current ownership; Guided artifacts retired; richer replay implementation supersedes the old donor. |
| Advisor recovery plan | Current release plan contains stronger wording; donor is stale. |
| p6b ACA docs | Current release ports guidance and adds current SSO/epoch/fix-on-fail improvements. |
| Backup and 5887 WIP checkpoints | Successor commits preserve old batch/RAG declaration scenarios; current tests strengthen attribution and add batch-rank coverage. The old interrupted implementation shapes are superseded. |
| Architecture pin | Merged, locked reproducibility anchor; preserved. |

## Per-branch file evidence

The companion JSON records every local ref, detached donor tip, unmatched/equivalent commit, changed file blob and local-file comparison from the original frozen audit. Automated `different_review` is a conservative mechanical classification: consult the dispositions above before interpreting it as missing work. The JSON deliberately retains raw measurement categories independently of the subsequent review verdict.
