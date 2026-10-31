# Composer+ carry-forward into .10

Current update after the 21 October ordinary frontend checkpoint: Composer+
HEAD is `52543bcc195419561b32412c79f4004e3fa8af62`, parent
`c299f2e7d852f897d0e34a1178aed4aa732cbd41`, tree
`e6cb88e8d1c11de4ea2ae653b5971201f6472c33`. Actual changed paths are exactly
`ComposingIndicator.tsx` and `ComposingIndicator.test.tsx`. Canonical branch-safety
reported FAIL=0/WARN=0 and ordinary Git hooks exited 0. The tested TSX bytes
match the retained 131-test/two-file ESLint scoped pass and 985e closure. No test
was replayed or custom controller launched for this normal checkpoint.

The guidance commit `c299f2e7` remains its parent, with exact two-Python guidance
bytes preserved. The staging set is empty, and the four unrelated plan documents
retain their pre-commit SHA-256 values. Those documents remain uncommitted.
TC-003/TC-007 canonical acceptance remains open; keyless REVIEW_REQUIRED and
signature limitations are unchanged. No .10 integration/rebase was performed.

The guidance-era Git comparison reported `c299f2e7...ce410353 = 1 1`, with merge-base
`67ad14c2`: the guidance commit is a concrete unique Composer+ commit to carry
onto the actual updated .9 checkpoint in an isolated integration. No rebase,
cherry-pick or integration was performed. Source planning is active; the
Continuation task draft is being completed with storage/client/frames companion
packets and a fresh Astra review. The .9 owner is running ordinary repository
tests; .10 integration/rebase waits for its actual GREEN and resulting checkpoint.
Independent work may proceed alongside it using available host resources, with
.9 priority at real CPU, memory, database or port contention. Bespoke test
controllers and checkpoint envelopes remain shelved.

The later .9 repair checkpoint is
`b003e0cd9c6c6a7b0968635a9c03f71a83bc2e73`, tree
`6a34e912debc469e9ce7e2048d433c08d5e606a9`; its canonical qualification is
running, not a completed GREEN. The selected Advisor carry is now concrete:
donor parent `2d5c0557e2d3dfcf6f176b94e9d17290d3da7a31` to
`e6b247943f911637f144fd11ba1c8130397fa2a9`, restricted to the seven paths in
the executable client plan's S7.2. Source inspection reproduced the owner's
47,088-byte patch exactly, SHA-256
`8af35dea091d2057c23ce1999fa2261252c01d62b07f1001ab22ec7100d5df9e`.
The final tip alone and the whole b09 branch are not that selected delta.
URL retention stays excluded. Curate the selected semantic hunks only on the
actual .9 GREEN base, preserving the new .9 corrections. No carry integration
has been performed.

The later test-only checkpoint is
`2e869240a388d1ede0454f1fbb9b019d22a9a9da`, tree
`a44b6334aa674c5868ef79b4c2035b5563e47efe`, parent b003. Its exact one-path
delta updates `tests/unit/web/sessions/test_prompt_guard_capacity_errors.py`;
production source is unchanged. Its full diff was inspected. Ordinary
qualification is continuing after runner/test corrections; no completed .9
GREEN is claimed and integration remains held.

## Approved .10 PromptShield implementation dependency

The coordinator selected the existing .9 writer's ordered route below for
proper .10 PromptShield behavior. That owner remains the single product writer,
with the existing LLM and systems reviewers refining the actual source seams.
This Composer+ task owns planning and integration reconciliation; it does not
duplicate those code changes. The route is an active dependency, not a claim
of implementation, passing tests or release acceptance.

**Mandatory fresh validation:** every content reuse, retry and resume requires
fresh validation for its actual use and assembled context, including when unit
identities and context appear unchanged. Stored IDs, hashes and prior receipts
retain identity and history only. They cannot skip required validation or
authorize a whole-graph CLEAN result for unseen content. Arbitrary paging or a
sampled prefix does not establish complete inspection coverage.

| Order | Selected increment | Required boundary |
| --- | --- | --- |
| P0 | Bind the actual updated .9 checkpoint/source disposition and isolated integration custody. | Retained owner source is an input, not a qualified commit. Preserve dirty work, source evidence and the separate frontend proposal. |
| 1 | Closed typed contracts for immutable units, actual use/context, coverage, validation and materialisation. | No clearance cache or identity-based validation bypass. |
| 2 | Bounded projections for both Composer and Advisor, including provider-native and rejected history. | Retain all required authority context; resolve the exact native-history projection in source. |
| 3 | Exact audited complete-value materialisation from immutable references. | Keep unsupported indivisible values losslessly stored; do not call unsupported internal review CLEAN. |
| 4 | Fenced atomic publication of content/version, entry applicability and state, with invalidation. | Persistence changes require the appropriate PostgreSQL crash/contention proofs and preserve/refuse contracts. |
| 5 | Authenticated, fenced, model-independent legacy repair/revert/replace/export. | Recovery must work without provider dispatch while preserving the Composer authoring and data-integrity invariants. |
| 6 | Both-role reuse, race, credential, audit, cancellation, hidden-content/no-CLEAN and legacy-recovery tests, followed by actual final qualification. | Every reuse/retry/resume validates the actual composition anew; isolated proofs are not combined release acceptance. |

Persisted attachment/version design and adapter-native history projection remain
concrete source decisions for that existing writer and its reviewers. They are
not requests for another user choice or authority for arbitrary segmentation.
The narrow explicit debug/unconfigured size-policy correction remains separate
from this durable route. URL retention, strategic credits, security activation
and deployment remain separately unreleased. This record grants no runtime or
additional writer, and does not authorize broad Continuation execution.

This planning record is outside the frozen frontend successor's source,
runtime, retained-evidence and external-file bindings. Recording this dependency
does not rebind or alter that packet.

The following is the retained historical source-only inventory observed
2026-10-21 11:14 AEDT. Its pre-checkpoint statements are superseded above.
It records custody and a
safe integration option; it authorizes no staging, commit, branch change,
rebase, cherry-pick, push, cleanup, test or deployment. The existing .9 owner
continues working. Its next concretely READY verification retains priority.

## Measured branch relationship

| Owner | Branch | HEAD | Raw index SHA-256 |
| --- | --- | --- | --- |
| Composer+ | `codex/composer-plus-20261020` | `67ad14c22b3987a16f0e9a71155102ee451da342` | `23b07c40dbb2491bf3a9127f1be1a941b58bf5a1df85b4fa639b13b08d646dfa` |
| .9 repair | `codex/composer-request-rejection-20261020` | `ce4103539fe97f07fc60fb0cd096256c207c8bc5` | `79812c17f6670aa678513b17ac83828600c68d4ea4cb4a62c1c4c8ae24b419a2` |
| Continuation planning | `codex/composer-continuation-plan-20261019` | `d647dd929d2b05be4918f175bc5ed96b18fdd198` | `e4e0b56cc352feca2df4db11500de776e57a2c8a7ce8cd8d573d72203efcd1ac` |

Read-only Git measurements returned:

```text
merge-base(67ad14c2, ce410353) = 67ad14c22b3987a16f0e9a71155102ee451da342
rev-list --left-right --count 67ad14c2...ce410353 = 0 1
Composer+-only committed changes = []
.9-only commit = ce4103539fe97f07fc60fb0cd096256c207c8bc5
Subject: Preserve rejection evidence in audit receipts and validation responses
Uncommitted path overlap between Composer+ and .9 = []
```

Therefore the observed committed .9 base already includes all committed
Composer+ history through 67ad. This is ancestry evidence, not proof that dirty
work is carried or that integration behavior passes. The dirty file digests
and modes matched on a second read during the inventory, and all three raw
indices remained unchanged. The .9 owner is actively editing; this is an
observation, not a freeze or an ownership transfer.

## Unique Composer+ product work to preserve

All four files remain unstaged at filesystem mode 0664. Only the first two
belong to the remaining ordinary guidance checkpoint proposal.

| Path | SHA-256 | Disposition |
| --- | --- | --- |
| `src/elspeth/web/composer/recovery_controls.py` | `574899e7aa158159e6f16a67cfb6982518a4acc7b6c214230c7092f0fe92e1a6` | TC-007 guidance; proposed exact two-Python-file checkpoint |
| `src/elspeth/web/composer/tools/secrets.py` | `b5e28f235174140c8a4ee90b5443cf26b9273894759814bd4be0c023857f312b` | TC-003 guidance; same proposed checkpoint |
| `src/elspeth/web/frontend/src/components/chat/ComposingIndicator.tsx` | `d8b7598ee23cc60dd1f2a2481e832fdb72112c3f09b5ab8cdbdbdee8017e910a` | Separate neutral waiting display change; retain its own qualification and future checkpoint |
| `src/elspeth/web/frontend/src/components/chat/ComposingIndicator.test.tsx` | `1b46e25a2ad7520fde533c7ee2ab065f8eb238df9fafc6b09a563ff7c143bf8e` | Accompanies that display change; excluded from guidance staging |

The proposed guidance checkpoint parent is 67ad, and its exact projected tree
is `afd97b96a87710722430f5f85b509b8823b603ff`. It has not been created.
Its retained source/test/scan evidence stays with the current owner; old failed
attempts, keyless REVIEW_REQUIRED/native1 and stale allowlist warnings remain
historical facts. None establishes qualification on a future .10 integration.

## Pending .9 state to retain with its owner

The measured .9 status contained 18 modified tracked paths and no staged or
untracked paths. Eight belong to the preserved signing/rekey donor set:

- `.agents/skills/judge-signature-workflow/SKILL.md`
- `docs/judge-signature-handoff.md`
- `elspeth-lints/src/elspeth_lints/core/cli.py`
- `elspeth-lints/src/elspeth_lints/core/cli_parser.py`
- `elspeth-lints/src/elspeth_lints/core/sign_bundle_transaction.py`
- `elspeth-lints/src/elspeth_lints/mcp/server.py`
- `tests/unit/elspeth_lints/test_cli_sign_bundle.py`
- `tests/unit/elspeth_lints/test_judge_mcp_server.py`

The other ten are the pending correction and regression work:

- `src/elspeth/web/composer/advisor_checkpoint.py`
- `src/elspeth/web/composer/tool_batch.py`
- `tests/helpers/required_work_reserve_recipes/reviewed-full-source-guards.json`
- `tests/unit/web/composer/test_tool_result_envelope_gate.py`
- `src/elspeth/web/composer/prompt_guard.py`
- `tests/unit/web/composer/test_pipeline_planner.py`
- `tests/unit/web/composer/test_prompt_guard_admission.py`
- `tests/unit/web/composer/test_prompt_guard_content_parts.py`
- `tests/unit/web/composer/test_prompt_guard_producer_parts.py`
- `tests/unit/web/composer/test_prompt_guard_wiring.py`

Keep all these bytes under their existing owner. A clean ce410 checkout lacks
these dirty corrections. The user's reported hotfix at his end is not a new
local commit established by this inventory. Select the actual updated .9
checkpoint only after that owner has reconciled and committed its intended
scope; do not substitute ce410 merely because it is currently committed.

## Documents and drafts to carry or retain

Composer+ has four untracked plan documents in `docs/superpowers/plans/`:

- `2026-10-20-composer-plus-development.md`
- `2026-10-20-composer-plus-frontend1.md` — retain failed/consumed first-attempt history.
- `2026-10-21-composer-plus-frontend1b.md` — preserve successor evidence; reconcile historical status before integration.
- `2026-10-21-composer-plus-guidance-obligations.md`

The separate planning branch has these seven untracked documents, all retained:

- `docs/design/composer-continuation-plan.md`
- `docs/design/composer-continuation-review.md`
- `docs/design/composer-continuation-astra-review.md`
- `docs/design/composer-plus-guidance-review.md`
- `docs/design/composer-plus-progress-review.md`
- `docs/superpowers/plans/2026-10-20-composer-continuation.md`
- `docs/superpowers/plans/2026-10-20-composer-continuation-storage.md`

The reviewed earlier design plan remains SHA-256
`354918236911912f7655437577b3192e40f86e0c11bf3816e9bbc7f12d19ac77`;
its Astra review remains
`0494ea0a1db58511b035314cc1d8636c08663176066e98944593f10b6ac371bc`.
The later writing-plans main document explicitly says PARKED DRAFT, incomplete;
the storage companion is not a complete assembled plan. Preserve both as
drafts. Their old source/schema assumptions require reconciliation before
future implementation, and the earlier review does not approve new .9 changes
or silently complete those drafts. Do not copy lane logs, installed packages,
private metadata stores or runtime artifacts into tracked .10 documents.

The durable configured PromptShield inspection/coverage and TOC/TOU solution
targets .10 under the coordinator's separate scope. It is not implemented by
the narrow pending explicit debug/unconfigured size-policy correction.

## Safe integration option after the ordinary checkpoint

1. Let the current owners finish their separately admitted work. Obtain the
   actual reviewed .9 checkpoint SHA and the exact two-Python guidance commit
   SHA, plus a separate frontend checkpoint if that change is selected. Verify
   their parents, changed paths, tests and remaining dirty ownership then.
2. Create one isolated integration worktree from the actual updated .9 SHA,
   with a new `codex/` branch. Leave existing shared and owner branches intact.
   Recheck ancestry rather than replaying Composer+ history already included.
3. Carry the exact guidance commit and selected frontend commit into that
   isolated branch. Resolve any semantic conflicts against updated .9 source;
   no whole-file replay, broad dirty copy or reset of another owner's index.
   If a change has no reviewed normal checkpoint, retain it with its owner
   until a separately authorized path-scoped integration is concrete.
4. Reconcile the selected durable documents against the resulting source in a
   separate docs change. Keep incomplete Continuation drafts clearly marked.
   Exclude private operational evidence and signing keys.
5. Qualify the resulting combined source with then-current focused and required
   integration gates under fresh serial admission. Existing isolated passes
   do not establish combined .10 acceptance. Obtain actual release/deployment
   target and authority separately; tonight's intent alone does not supply it.

This option requires no shared branch rewrite. No rebase or cherry-pick was
performed by preparing this document, and none is authorized by it.
