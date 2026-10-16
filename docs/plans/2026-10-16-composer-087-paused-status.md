# Composer 0.8.7 — current status and continuation point

**Active after verified continuation. Preserve the current checkout and evidence.**
The 00:06 UTC checkpoint reprocessed the original pause after compaction; it was
not evidence of a new direct instruction. Its original text is preserved in
the ignored lane file `pause-checkpoint-20261016T0006.md`.

The chronology is explicit:

1. Direct pause `01a140f6-df63-7ac2-bf00-c51f6cb32f67` has UUIDv7-derived time
   **2026-10-15 19:08:03.299 UTC**. The API provides no separate message timestamp.
2. John's later **19:11:41 UTC** message
   `Sentinel_45dc3ea305d081919a36fdb9f7503db3` instructed keeping .7 working if
   healthy. Emmy and the existing relay verified and conveyed this continuation.
3. John's later `Sentinel_341d709504f08191af4f992536304a4a` asked how far .7 was
   through its list. A status question does not revoke the continuation.
4. Compaction `01a14200-9cbf-78d1-b0df-befe2f4a9916` preceded the repeated pause.
   **00:06 is a checkpoint write time, not a new instruction time.** The supported
   latest completed-turn read contained no new direct user message.
5. Emmy explicitly reconfirmed full approved continuation in current turn
   `01a14210-cc80-75e0-8b31-15517474ced3`. A fresh supported read finds no new
   direct user message. The old owner remains retired and .6 remains separately
   held. This owner continues after fresh source/process/capacity checks.

Obey an actually newer direct stop, identified by its message and chronology.
Do not treat repeated pre-compaction wording or a status-write timestamp as a
new stop. Emmy and the relay retain John's confirmed direction authority.

The snapshot below was captured **2026-10-16 00:04:29 UTC / 11:04:29 Canberra**.
Its measurements remain historical. No product check or implementation ran during
that mistaken checkpoint; only stop notices, status reads and documentation did.

## Checkpoint history and continuing ownership

- All three current lanes were already completed when inspected:
  `/root/implementation`, `/root/astra_review`, `/root/astra_obligations`.
  Each received a notice to remain stopped. No new agent was created or resumed.
- A controlled host census at **00:03:53 UTC** reported ELSPETH pytest allocation
  `0` and JavaScript test runners `[]`. Four unrelated Keisei serial pytest
  masters were observed and left untouched. No process signal was necessary.
- The sole integrator remains thread `01a140e3-fc15-7900-b0c5-cc80ebc95fb2`.
  Root owns Git, the ledger and runtime allocation. The same implementation
  writer and two independent reviewers remain the assigned lanes for any resume.
  Do not revive the superseded original .7 owner or create a replacement owner.
- The separate .6/release lane, shared services and session-lock owners were not
  changed. This checkpoint does not claim to pause unrelated owners.

## Checkout to preserve

- Branch: `codex/composer087-contracts-20261015`.
- HEAD: `3f2db4f0e92958ecf40afd58fcc6383f2c90521f`.
- `git ls-files --unmerged` returned no entries. `CHERRY_PICK_HEAD`, `MERGE_HEAD`
  and `REBASE_HEAD` verification each exited `1` with empty output; no unfinished
  operation was observed. In particular, the old unfinished cherry-pick recorded
  in an earlier checkpoint has since completed.
- No staging, commit, push, merge, reset or cleanup was performed for this pause.

Raw `git --no-optional-locks status --short` at the checkpoint:

```text
 M docs/plans/2026-10-15-composer-087-work-ledger.md
 M docs/plans/2026-10-16-composer-087-paused-status.md
 M src/elspeth/web/composer/error_codes.py
 M src/elspeth/web/composer/tools/generation.py
 M src/elspeth/web/composer/tools/transforms.py
 M tests/unit/web/composer/test_splice_transform_tool.py
?? tests/unit/web/composer/test_transform_rejection_contracts.py
```

That historical snapshot has two root documentation paths and five packet paths.
The two later gate-test repairs add `test_validation_guidance_catalogue.py` and
`test_validation_error_codes.py`, making nine currently owned paths. The new
contract test remains untracked until staging. Preserve all nine; do not stash,
restore, clean or stage unrelated state.

## Completed local integrations

Current `git log -8` verifies this ancestry. These are local integrations;
remote merge, protected CI, signing, release and deployment are separate gates.

| Commit | Completed scope |
|---|---|
| `620231a951b9bbc7fe28e3cc6b0e9ab38ebb898e` | Explicit candidate/full-replacement rejection meaning, safe guidance and shared resolver fixes. |
| `9d8b43b962d733e9fe65ac1de1b088096a1cc731` | Shared-link fixes SL1–SL4: malformed/private route intent, authentication destination and stale shared snapshot. SL5 remains open. |
| `208bf8d778344ff26583c3917b01a2d491f0b3b0` | Saved-graph output refusal evidence, codes, subjects and credential payload. |
| `32673a1b7f6572c7636bafbc57f653c8899ed99e` | Disable hidden auto-title SDK retries; physical transport/accounting evidence retained. |
| `9d6533fe07ef0cdb5270611cc4f1b343cc39de5f` | Combined quota provisioning and Sessions epoch 77, including predecessor refusal and cancellation ownership. |
| `41348ede3c667bf337ab6597a59df0f9e02dc5a5` | Source refusal/integrity ownership, pure policy admission, plural resolver and census repairs. |
| `3f2db4f0e92958ecf40afd58fcc6383f2c90521f` | Preserve admitted output schema faults and repair source/output provider doubles with real-method autospec. |

Existing completed receipts were read, not rerun. Direct XML parsing and exit
files produced the following raw measurements; all apply to their recorded
historical inputs, not to a newly executed whole-branch run:

| Receipt prefix | Exit | Passed | Failures | Errors | Skips | Frozen |
|---|---:|---:|---:|---:|---:|---|
| `transform-splice-root-positive1` | 1 | 293 | 2 | 0 | 0 | true |
| `output-schema-root-positive1` | 0 | 85 | 0 | 0 | 0 | true |
| `output-schema-root-gates1` | 0 | 448 | 0 | 0 | 0 | true |
| `output-schema-root-postgres1` | 0 | 18 | 0 | 0 | 0 | true |
| `packet1c-root-positive2` | 0 | 104 | 0 | 0 | 0 | true |
| `packet1c-root-gates2` | 0 | 436 | 0 | 0 | 0 | true |
| `packet1c-root-postgres1` | 0 | 40 | 0 | 0 | 0 | true |

The saved source packet also records 78 intended negative failures across 21
regressions; the output follow-up records 20 intended failures across its three
control runs. Their existing reviewer acceptance and exact activation/case-set
proofs remain in the lane directory. This pause did not rerun those controls.
Both the output and transform keyless comparisons retain a nonzero 3277-finding
corpus with zero semantic additions/removals; exit `1` is recorded. This is not
operator signature clearance.

## Transform/splice: bounded acceptance, local integration next

Both final independent reviews accept freeze4 for the bounded packet. All128
bindings match before this root documentation update. Normal hooks and local Git
integration are next; whole-plan, broad gates, signing and release remain open.

- Original positive run remains **red: exit 1, 293 passed, 2 failed, 0 errors or
  skips, frozen**. Both strict replay tests received `plugin_options_invalid`
  because their empty fixed schema was invalid before replay comparison.
- Review found the same invalid premise in an inherited atomicity test. Its
  generic refusal assertion had passed without proving replay-conflict reach.
  This is a test defect, not evidence requiring a production guard-order change.
- Both functions now use observed schema plus `required_input_fields=["text"]`,
  assert actual contextual prevalidation succeeds, and require the precise
  `splice_replay_conflict`. Production bytes are unchanged by this repair.
- All **three replay cases passed freshly**: the new handler and dispatch cases
  plus the inherited atomicity case. The other **292** historical passes retain
  bounded evidence under controlled complete-module/production proofs. They are
  not newly executed passes and do not turn the original295-case run green.
- Original gates1 remains red:566 passes and2 stale test failures. Its count
  fixture assumed one raw error; its legacy candidate fixture supplied no code.
  Both functions were repaired without production changes and passed freshly.
- The complete affected gate selection passed **568** cases. All **13** negative
  families produced **147** intended witnessed failures, with zero errors/skips/
  unintended passes. Explicit serial focused PostgreSQL passed **18** cases.
  Final runtime/reuse reviews are complete. No runtime process remains active.
- Completed Ruff check/format, affected mypy/contracts and unchanged-production
  keyless evidence retain only their recorded scope. The explicit final
  production format check exited `0` with `3 files already formatted`.

The unchanged production implements saved-graph refusal semantics across
upsert, option patch and splice preparation: retain standing findings, preserve
exact policy/profile codes and admitted identity, propagate admitted schema
faults, publish static safe provider-path guidance and preserve full credential
repair payloads. Splice retains attributed counterfactual findings alongside
standing saved findings, atomicity and exact replay behavior. No public wire,
subject grammar, schema epoch, policy or broader caller family is expanded.

Accepted stopped manifest: `transform-splice-freeze4.json` in the ignored lane,
raw SHA `505a6f7763f893dbd37b91830211f1e767ff13196bfc6fb2d43e3d6d5aefe5e2`.
All16 fresh runs share6644 identical before/after/current project records,
SHA `2eb81fabdc4239421b865ca25ca2a76454e7bb52aac93e9cd9d3f6d3de6c02a7`.
Root's final-runtime-bindings proof also verifies exact292 historical cases plus
fresh replay3, complete-module repairs and1968 unchanged production inputs.
This status/ledger update intentionally changes only the two documentation
bindings; historical manifests/receipts remain intact. A later preflight-only
secret-scan marker changes the new credential test's comments/layout while its
complete-module AST remains identical. Verify all125 other bindings, that controlled
comment-only overlay and the staged bytes before integration. The original
executed test module and leaf-line evidence are preserved separately.

| Preserved product/test path | SHA-256 |
|---|---|
| `src/elspeth/web/composer/tools/transforms.py` | `c2acf0c6e42b50a8968b9df71073e9ec5bc2f8a01fa7786c62e71da4832f4434` |
| `src/elspeth/web/composer/tools/generation.py` | `506d0d8a737189097bf32cf8760142b9f65eb97142b9a65e95f5cd12e65146b1` |
| `src/elspeth/web/composer/error_codes.py` | `5432330e7bc36fd3f188d43c5a81fde17d7ba704bb77c87a9afd58822ccb7ccc` |
| `tests/unit/web/composer/test_transform_rejection_contracts.py` | `63efd943ad2ceddbd5570d4e3a7ace85ad8508af29d2611f5fd306c584cd9f88` |
| `tests/unit/web/composer/test_splice_transform_tool.py` | `2632289836b54b4508018ba31dd8f5bb2359faadc4f66b34f57fc46f778fb1a3` |

The original red receipt, executed originals, two-module reconstruction,
1968-input production comparison and 292-case reuse set are preserved. Important
ignored files under the lane directory:

- `transform-splice-result.md` and `transform-splice-repair-proof.json`.
- `transform-splice-root-positive1-{receipt,summary}.json`, original `.xml` and log.
- `transform-splice-root-reuse-case-set.json` and `transform-splice-repair-before/`.
- `reviews/astra-transform-splice-integration-review.md` and
  `reviews/astra-transform-splice-systems-review.md`.
- `transform-splice-root-keyless1-delta.json` and
  `transform-splice-keyless-baseline-proof.json`.
- `pause-current-snapshot.json` and `pause-current-capacity.json`.

## Current full worklist

“Accepted locally” describes the bounded reviewed integration above. Items 0–15
and discovered fixes remain active in the approved scope; 16–18 remain conditional.

| Planned work | Current status |
|---|---|
| 0. Baseline inventories and ownership | Complete; live registry/profile and direct-call inventories recorded. |
| 1. Rejection identity, operation meaning and closed feedback | Candidate/full-replacement, output and source groups accepted locally in `620231a`, `208bf8d`, `41348ede` and output schema follow-up `3f2db4f0`. Transform/splice has final bounded acceptance from both reviewers and completed runtime evidence; normal local integration is next. Graph wiring, blob, secret, discovery and remaining dispatch migrations remain pending. |
| 2. Saved-but-blocked persistence/reload/UI | Pending joined behavioral evidence; inherited .6 teaching retained. |
| 3. Model-catalog provenance | Fixed in .6; .7 regression closure pending. |
| 4. Explicit CSV failure choice | Runtime refusal already exists. False assistance claim that omission defaults to discard still needs correction and its provenance checks. |
| 5. Scoring scale, anchors and meaning | Approved together in the existing criterion reviewed value. Systems representation/rendering design complete; implementation pending. |
| 6. Public argument admission | Characterization pending; no public bypass established. |
| 7. Typed services and faithful test doubles | Typed production owner exists. Real-signature/context-forwarding characterization pending. |
| 8. Source proof context and unavailable content | Approved terminal readiness-blocked behavior; concrete systems design complete, implementation pending. |
| 9. Fresh settlement versus historical review-debt repair | Ownership/placement reviewed; debt query, repair placement and unnecessary exclusive-lease correction pending. |
| 10. Persisted record admission | Narrow mixed singular/plural-source refusal approved, including empty plural maps; compatibility characterization and implementation pending. Supported unambiguous forms must remain. |
| 11. Checked, failed and skipped validation phases | Pending honest phase/next-action behavior; no new phase schema or repair credit approved. |
| 12. Source-demand capacity refusal | Pending stable normalization through preflight, completion, surfacing and persistence; preserve completed work and fail closed. |
| 13. Tool-family lifecycle coverage | Actual tool map complete; broader success/refusal/retry/cancellation/privacy/reload evidence pending. |
| 14. Plugin/profile qualification and repair | Registry inventory complete; case-specific construction/parity and repair checks pending. Inventory is not live-provider qualification or closure of #195. |
| 15. Lifecycle/API/UI outcome distinctions | Broader rejected, saved-blocked, pending, validated, executable and executed witnesses pending. |
| 16. Repair-credit policy | Conditional; not started. |
| 17. Request-local source-demand cache (#246) | Conditional; not started. |
| 18. Validation fairness/admission (#249) | Conditional; not started; no authorization defect established. |

## Findings discovered along the way

The following inventory is carried from the working ledger, with the completed
output schema integration corrected to its current committed state.

| Finding | Current status |
|---|---|
| Rejection policy reasons flattened; admitted schema faults masked | Candidate/shared-resolver packet accepted locally. Output policy reasons and saved identity are fixed; its separate two-caller schema-fault follow-up is committed locally as `3f2db4f0`. Other caller families remain pending. |
| Migrated candidate paths disclose raw path details | Fixed locally with static safe guidance and deliberate privacy controls; output path/history cases also accepted. |
| Repeated masked-classifier guidance falsely says the request is impossible | Fixed locally in packet 1a. |
| Inline CSV parse, scan-cap and content-conflict refusals conflated | Fixed locally in packet 1a. |
| Uppercase quota validation codes miss their guidance | Fixed locally by case-insensitive lookup in packet 1a. |
| Changed CSV reader authority binding | Rebound and controlled locally in packet 1a; separate pre-existing global writer-inventory expected failure remains unqualified. |
| Output missing-target code/subject and standing saved findings lost | Fixed locally in packet 1b, retaining credential-repair payload and admitted schema identity. |
| Schema-augmentation AST tripwire misses migrated saved-graph helper | Fixed locally; actual missing-identity mutation now fails the owning gate. |
| Q1: configured container token ceiling is not provisioned | Accepted locally in combined integration `9d6533fe`; focused checks, independent/system reviews and normal hooks complete. Broader gates remain separate. |
| Q2: automatic titles make hidden SDK retries | Applied locally as `32673a1`; bounded combined integration accepted, retaining the physical-transport/accounting evidence and provider limitations. |
| Q3: token-only container policy incorrectly needs a storage cap | Combined candidate accepted; optional container storage and mandatory identity storage verified within the focused scope. |
| Q4: active container singleton is keyed by actor | Checked constant-slot fix accepted; focused PostgreSQL quota authority cases pass. Full PostgreSQL qualification remains due. |
| Q5: new startup provisioning can outlive database owners on cancellation | Retained-worker join accepted with unchanged production/cancellation receipts; preserve deferred exception propagation and independent recovery. Broader qualification remains due. |
| Quota standalone epoch 76 collides with .6 semantic epoch 76 | Combined epoch 77 accepted with retained graph-review semantics, both predecessor refusals and witnessed negative controls. No user-store conversion/reset is authorized. |
| Three quota startup R4/R6 deferred-propagation lint findings | Source reviewed without a product blocker; repository adjudication/operator signing remains open. Correct lifecycle code is retained. |
| SL1: malformed shared route opens recipient workspace | Fixed and accepted locally in `9d8b43b`. |
| SL2: unavailable private link loses intent and resumes recipient workspace | Fixed and accepted locally in `9d8b43b`, including navigation and authentication remount. |
| SL3: stale authentication destination A overrides explicit B | Fixed and accepted locally in the same shared-link packet. |
| SL4: previous shared snapshot remains under a new token | Fixed and accepted locally in the same packet using route identity. |
| SL5: shared-view global keyboard shortcuts act on recipient workspace | Deferred; the claimed new relay authorization was retracted as historical. No SL5 implementation or test run started. New-session shortcut reproduced; validation source-supported only. No sender mutation or cross-user access established. |
| Ready plural source blob with NULL hash is treated as repairable | Corrected locally in `41348ede`; real corruption and laundering controls preserve the owned integrity failure. |
| Invalid stored MIME has a redundant recoverable branch; MIME DB-CHECK comment is false | Corrected in source1c; the real row reader retains MIME integrity ownership, and the comment names the actual write/read guards. No constraint or epoch change. |
| Source policy refusal occurs after schema access | Corrected in source1c with pure policy admission before direct schema access. Actual unavailable reasons and admitted ValueError/KeyError propagation are qualified separately. |
| Null/malformed source claims disappear or are string-coerced | Included in approved item-8 design; implementation pending. |
| Missing proof context is conflated with no authored source claim | Included in approved item-8 design; implementation pending. |
| Metadata-only source proof permits later unlocked content fallback | Included in approved item-8 design; implementation pending. |
| Source unavailability reaches completion too late or becomes false budget exhaustion | Included in approved item-8 design; implementation pending across preview/completion/review entrances. |
| Per-query scoring override does not inherit node criterion references | Actual boundary identified; teaching/characterization pending. No new override mechanism approved. |
| LiteLLM conditional parameter repair/fallback can differ from ordinary retries | Separate documented limitation; title retry pins do not universally qualify provider behavior. |
| Output admitted-schema ValueError/KeyError becomes availability repair | Two-caller migration accepted locally as `3f2db4f0` after both reviews and normal hooks. Actual handler/dispatch/turn positives, relevant gates, three witnessed controls, focused PostgreSQL and fresh keyless comparison complete; broader gates remain due. |
| Aggregation output-count refusal hides standing saved findings | Actual saved-findings behavior, complete consumer guidance and deliberate regression control are accepted; local integration is next. |
| Transform provider-path refusal exposes private path/root text | Static safe projection and actual private-path canary control accepted across upsert, patch and splice preparation. |
| Node refusals omit codes/subjects or mask admitted schema faults | Final source/runtime reviews accept precise code/subject, admitted identity and fault propagation with the completed controls. |
| New predecessor test bypasses the required session-engine factory | Normal hook exposed it; repaired with the existing read-only inspection factory. All39 epoch cases plus masquerade gate pass, and actual initializer-admission mutation still fails as intended. Normal hooks now pass. |
| Credential-envelope census still names the legacy adapter instead of its payload builder | Corrected at the real builder with explicit per-row constructor ownership. The complete affected gate and witnessed key/missing-payload/owner controls pass their intended checks; no wire expansion. |
| Source contract tests contain invalid fixture and outer-fault assumptions | Repaired with real owner premises and accurate outer/endpoint oracles; all104 affected cases pass. The original46 failures and2 teardown errors remain historical red evidence, not new product defects. |
| Source/output turn tests use unspecced provider mocks | Repaired with autospec of the actual bound provider method. All four affected source turn cases and the output turn cases pass, along with the owning mock-discipline gate. Earlier source1c receipts retain their original scope; no new production defect established. |
| New and inherited replay fixtures fail before replay comparison | Both functions repaired; all three affected cases passed freshly. Exactly292 untouched historical passes have controlled reuse; original red receipt retained. |
| Count-guidance and legacy full-replacement gate fixtures encode stale contracts | Both functions repaired with real operation owners and exact assertions. Focused2 and owning568 cases pass; original566-pass/2-failure receipt remains red. |
| Negative-control runner reads only the final digit of multi-digit selections | Corrected in the ignored root runner. Controlled proof and all13 actual transform selections succeed as intended; prior single-digit receipts remain valid. |
| Secret scanner flags the deliberate credential canary | Documented per-line marker/formatting only; full-module AST unchanged and scoped Ruff/format pass. Historical exact-source receipts retained. Normal preflight recheck is next. |

The broader profile `ValueError` boundary, residual service advisor-context
elision comment and global Sessions writer expected failure remain observations
requiring characterization. They are not newly confirmed product defects.
The deployed shared-link/streaming cause and cross-user mutation remain unproven.
Title retry evidence does not universally qualify LiteLLM fallback/repair behavior.

## Worktree cleanup

The authorized cleanup completed before this pause. Its saved execution report
records **21 merged clean worktrees removed, registry 71 → 50**, with all selected
commands exiting `0` and directory/registration absence verified. Four merged
worktrees containing substantive ignored local state were retained; branch refs
and protected owners were preserved. This is the completed cleanup receipt, not
a fresh registry measurement in this checkpoint. No further cleanup is authorized
by the pause. See `worktree-cleanup/report.md` in the ignored lane directory.
Concurrent disk changes must not be attributed entirely to that cleanup.

## Continuing execution point

1. Explicit continuation has been received from Emmy. Preserve the same owner,
   current HEAD and nine owned paths, including both repaired gate-test files.
   Do not revive the retired .7 thread or duplicate .6. Preserve the explicit
   chronology above in future handovers.
2. Recheck host capacity and runtime custody. Preserve app/session locks and
   unrelated processes. No capacity reservation persists through this pause.
3. Both final reviews, repaired replay/gate checks, owning gates,13 exact negative
   families, focused PostgreSQL and unchanged-input/reuse verification are complete.
   Preserve all original red and passing receipts; do not duplicate these runs.
4. Verify the two intentional root-document changes and all other frozen bytes,
   stage only the nine owned paths, run normal branch preflight/hooks and integrate.
5. Reverify the actual new HEAD/committed blobs and update current status. Then
   dispatch the same writer on the existing graph-wiring brief/design, after
   revalidating its selected declaration hashes against the integrated source.
   Continue the remaining approved plan;16–18 remain conditional and SL5 deferred.
6. .7 delivery is the priority. The .8 streaming/advisor initial note is bounded
   exploration only, preserved under `streaming-evaluation/report.md` in the
   ignored lane. Its agent is stopped; no .8 implementation is authorized.

Whole-branch default/full PostgreSQL gates, final provider/native/UI qualification,
protected exact-head CI, PR/merge, operator adjudication/signing and release/live
acceptance remain open. No PR was created by this thread. No spending, deployment,
user-store migration/reset/conversion, access change, protection bypass, operator
key handling or session-lock clearing was authorized or performed by this pause.
Do not introduce a new provider bypass or tutorial-specific path on resume.
