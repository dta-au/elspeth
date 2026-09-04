# U3 — how much of `3f3857d20` is still unique against `a2176dfe2`?

Measured 2026-09-04 from the shared checkout (repository root)

> **Re-verified at `release/0.8.0` @ `cbae1ef0c` (2026-09-04 rebaseline).**
> Every finding below holds unchanged. The two comparands — `3f3857d20` and
> `a2176dfe2` — are fixed refs, and each mainline-dependent claim was re-run:
> `resolve_pending`, `SessionInterpretationResolutionCommand`,
> `SessionInterpretationValidationInputs` and
> `_SessionInterpretationResolutionPlanner` still return **zero** files on both
> branches; `interpretation_validation.py` and
> `test_interpretation_validation_inputs.py` are still **absent from both**; and
> `git rev-list --count 64b7d144e..feature/deferred-platform-recovery --
> src/elspeth/web/sessions/routes/interpretation.py` is still **0**, so harvest
> item 3 still applies with no reconciliation. The verdict stands: the step-8
> list of three is **TOO NARROW**.

(`release/0.8.0` @ `91816d0f3`). Read-only: no test run, no commit, no branch
touched. Revs used throughout:

| alias | rev | what |
|---|---|---|
| BASE | `64b7d144e18fc338677cb69f70d99e5d44dafef8` | parent of the WIP; merge base |
| WIP | `3f3857d20fdec645d41dbd62dd7e430fc13b03b9` | `recovery/deferred-platform-wip-broken` |
| PLAT | `a2176dfe225bd9d56d5eed642f7a8aff0a9485d2` | `feature/deferred-platform-recovery` (verified `git rev-parse`) |
| MAIN | `91816d0f39808271bda1dced212db7717943827c` | mainline `release/0.8.0` |
| LANE | `4c59c9d029daaeb5d2ab95911289e315ff6ee0f4` | `recovery/deferred-platform-completion` tip, merged into PLAT |
| HEADSIDE | `7cd2fc6db08714386bfb7e9d1ddd9b012f8c589d` | `feature/unified-lineage`, the other parent of the merge |
| MERGE | `0b676d1954514f9fff3e0164c035dd0ab43dee83` | the 3-way merge comment 8915 describes |

---

## VERDICT: the step-8 list of three is **TOO NARROW**.

The list is not wrong about what it names — all three items were verified
present-and-absent exactly as the brief claims. It is incomplete in three
independent ways, each measurable:

**(a) The brief's own source document names FOUR deliverables, not three.**
`PLAT:docs/superpowers/plans/2026-08-02-multi-replica-platform-pause-handoff.md`
lines 220–232 ("Chosen correction and current WIP") list what the interrupted
implementer began:

```
- added `interpretation_validation.py` ...                       <- step 8 item 1
- added focused parity/mutation/hidden-carrier tests ...          <- step 8 item 2
- moved more interpretation resolution work behind repository authority; and   <- NOT IN STEP 8
- changed the route so an ordinary exception during post-commit ... <- step 8 item 3
```

Step 8 drops the third bullet. That bullet is the largest unique body in the
commit and is absent from both branches (evidence below).

**(b) Items 1 and 3 are not separable from the dropped bullet.** Both are
*wired through* the repository-authority move:
- The DTO is adopted at the protocol surface by *replacing* the validator
  argument: `PLAT:protocol.py:3030-3034` has
  `create_or_reconcile_pending(self, command, validator: SessionPendingInterpretationValidator)`;
  `WIP:protocol.py:3017` has
  `create_or_reconcile_pending(self, command, validation_inputs: SessionInterpretationValidationInputs)`
  plus a new `resolve_pending(...) -> SessionInterpretationResolutionResult`.
  Harvesting `interpretation_validation.py` as a free-standing file gives a DTO
  nothing calls.
- Item 3's hunk passes `session_operation_context=compose_operation_lease.context`
  to `service.resolve_interpretation_event` — a parameter `PLAT`'s signature
  (`PLAT:service.py:7381-7392`) does not have. It applies textually (the file is
  byte-identical BASE↔PLAT) but does not typecheck against PLAT.

**(c) The proof obligations step 8 itself demands are in files it does not
name.** Step 8 says "Scope the DTO to closure and its RED/GREEN proof". The DTO
closure pin
(`test_interpretation_planners_use_closed_validation_inputs_without_graph_scanners_or_callbacks`)
lives in `test_operation_fence_wiring.py`, and the cleanup-primacy RED/GREEN
(`test_precommit_resolution_failure_remains_primary_when_release_also_fails`,
`test_committed_resolution_logs_release_failure_without_masking_receipt_or_mutating_twice`)
lives in `test_interpretation_events_routes.py`. Neither file is in the list of
three; both tests are absent from PLAT (grep count 0 over `-- tests`).

Nothing in the list of three is TOO WIDE: each of the three was verified unique
and wanted.

---

## Correction to the brief's arithmetic (§3 and U3)

U3 says "the 1,827-line tracked refactor". `git diff --name-only BASE WIP` returns
**16** paths, not 14, and `git diff --stat BASE WIP` is **2,342 insertions /
602 deletions**. The 14/1,827/`baa76f8b…` figures are the *tracked-at-capture-time*
subset, which excludes the two then-untracked new files
(355 + 160 = 515 lines, 0 deletions; 1,827 + 515 = 2,342 exactly). Verified:

```
$ git diff 64b7d144e 3f3857d20 -- <the 14 non-new paths> | sha256sum
baa76f8bbc31059e18d0aee8cf551151ecbffbd4a5417481e10cef1927676ea4  -
$ git diff --shortstat 64b7d144e 3f3857d20 -- <the same 14>
 14 files changed, 1827 insertions(+), 602 deletions(-)
```

`PLAT:…-platform-pause-handoff.md:158-183` states this explicitly ("fourteen
tracked files … two untracked files, making sixteen dirty paths"), so §3 is
faithful to the handoff — but U3's phrase "the 1,827-line tracked refactor in
`3f3857d20`" understates the slice by 515 lines. Say **2,342/16** when scoping
step 8.

---

## Per-path table

`unique_lines` = whitespace-normalised, de-duplicated added lines
(`git diff -U0 BASE WIP -- <path>`, `^+` minus `+++`) that appear **nowhere** in
PLAT's versions of all 16 paths (union corpus, so a helper moved between files
still counts as present). It is a floor-ish proxy, not a semantic count; the
classification below rests on the symbol-level greps, not on this number.
Script: `u3/uniq.sh`; artefacts under `u3/`.

| # | path | added | uniq vs PLAT | uniq vs MAIN | classification |
|---|---|---|---|---|---|
| 1 | `src/elspeth/web/coordination/repository.py` | 126 | 65 | 90 | UNIQUE_AND_WANTED |
| 2 | `src/elspeth/web/sessions/interpretation_validation.py` | 270 | 233 | 245 | ABSENT_FROM_BOTH |
| 3 | `src/elspeth/web/sessions/pending_interpretation.py` | 247 | 116 | 180 | UNIQUE_NEEDS_RULING |
| 4 | `src/elspeth/web/sessions/protocol.py` | 82 | 56 | 72 | UNIQUE_AND_WANTED |
| 5 | `src/elspeth/web/sessions/routes/interpretation.py` | 48 | 19 | 26 | UNIQUE_AND_WANTED |
| 6 | `src/elspeth/web/sessions/service.py` | 37 | 13 | 25 | UNIQUE_AND_WANTED |
| 7 | `tests/integration/web/composer/test_guided_interpretation_run_backstop.py` | 14 | 3 | 7 | UNIQUE_AND_WANTED |
| 8 | `tests/integration/web/composer/test_interpretation_runtime_handoff.py` | 19 | 3 | 9 | UNIQUE_AND_WANTED |
| 9 | `tests/unit/architecture/test_session_db_mutation_authority.py` | 104 | 90 | 101 | UNIQUE_NEEDS_RULING |
| 10 | `tests/unit/web/composer/test_compose_loop_persistence.py` | 17 | 3 | 8 | UNIQUE_AND_WANTED |
| 11 | `tests/unit/web/composer/test_request_interpretation_review_tool.py` | 46 | 26 | 38 | UNIQUE_NEEDS_RULING |
| 12 | `tests/unit/web/sessions/test_interpretation_events_routes.py` | 84 | 45 | 52 | UNIQUE_AND_WANTED |
| 13 | `tests/unit/web/sessions/test_interpretation_events_service.py` | 219 | 137 | 167 | UNIQUE_AND_WANTED |
| 14 | `tests/unit/web/sessions/test_interpretation_validation_inputs.py` | 103 | 68 | 72 | ABSENT_FROM_BOTH |
| 15 | `tests/unit/web/sessions/test_operation_fence_wiring.py` | 76 | 43 | 69 | UNIQUE_NEEDS_RULING |
| 16 | `tests/unit/web/sessions/test_static_direct_writers.py` | 16 | 9 | 11 | UNIQUE_AND_WANTED |

**Nothing is ALREADY_PRESENT at path granularity.** The only already-present
fragment found anywhere in the slice is one of the two `ReviewedWriter` rows
path 16 adds (see below).

### Existence check (the step-8 premise), `git cat-file -e <rev>:<path>`

| path | BASE | WIP | PLAT | MAIN |
|---|---|---|---|---|
| `src/elspeth/web/sessions/interpretation_validation.py` | absent | **exists** | **absent** | **absent** |
| `tests/unit/web/sessions/test_interpretation_validation_inputs.py` | absent | **exists** | **absent** | **absent** |
| `src/elspeth/web/sessions/routes/interpretation.py` | exists | exists | exists | exists |

`git diff --numstat 64b7d144e:src/elspeth/web/sessions/routes/interpretation.py
a2176dfe2…:src/…/routes/interpretation.py` prints **nothing** — the file is
byte-identical BASE↔PLAT, which independently confirms step 8's
`git rev-list --count 64b7d144e..feature/deferred-platform-recovery -- <that file>`
= **0** (re-run, output `0`). **All three step-8 premises hold.**

### Symbol-level uniqueness (`git grep -w -l <sym> <rev> -- src tests`, file counts)

| symbol | PLAT | MAIN | WIP |
|---|---|---|---|
| `SessionInterpretationValidationInputs` | 0 | 0 | 5 |
| `build_interpretation_validation_inputs` | 0 | 0 | 2 |
| `_DetachedValidationCatalog` / `_DetachedValidationProfiles` / `_DetachedPluginValidationEntry` / `_DetachedAliasLowering` | 0 | 0 | 1 each |
| `validate_composition_state_with_interpretation_inputs` | 0 | 0 | 3 |
| `resolve_pending` | **0** | 0 | 8 |
| `SessionInterpretationResolutionCommand` | **0** | 0 | 6 |
| `SessionInterpretationResolutionResult` | **0** | 0 | 4 |
| `SessionInterpretationResolutionSnapshot` | **0** | 0 | 4 |
| `SessionInterpretationResolutionValidationCandidate` | **0** | 0 | 3 |
| `SessionInterpretationResolutionValidationResult` | **0** | 0 | 4 |
| `_SessionInterpretationResolutionPlan` / `Planner` / `Validator` | **0** | 0 | 2/2/4 |
| `_forbidden_validation_dependency` | 1 (the pin only) | 0 | 2 |
| `get_referents` | 0 | 0 | 2 |
| `_SessionPendingInterpretationValidator` | 4 | 0 | 3 |

All **23** `def test_*` names the WIP adds return grep count **0** on PLAT.

---

## Per-path evidence and rulings

**1. `src/elspeth/web/coordination/repository.py` — UNIQUE_AND_WANTED.**
+170/-0 adding `_RepositoryInterpretationMutations.resolve_pending` (hunk
`@@ -1049,6 +1056,169 @@`). `git show PLAT:…/repository.py | grep -n "def
resolve_pending"` exits 1 (no match); `create_or_reconcile_pending` is at
PLAT:782. This is the handoff's dropped third bullet.

**2. `src/elspeth/web/sessions/interpretation_validation.py` — ABSENT_FROM_BOTH.**
New file, 355 lines: `SessionInterpretationValidationInputs`,
`build_interpretation_validation_inputs`,
`validate_composition_state_with_interpretation_inputs`, and the four
`_Detached*` adapters. `git cat-file -e` fails on PLAT and MAIN. **Step-8 item 1
premise holds.** Caveat: it is *dead* without path 4's protocol swap.

**3. `src/elspeth/web/sessions/pending_interpretation.py` — UNIQUE_NEEDS_RULING
(two disjoint bodies in one file).**
Hunk split (`u3/wip__…pending_interpretation.py.diff`):
- hunks `@@ -1239 @@`, `@@ -1258 @@`, `@@ -1275 @@`, `@@ -1348 @@` — ~+40/-24 —
  extend `_forbidden_validation_dependency` (adds `get_referents`, `partial`,
  a `deque` worklist, a 500 000-object budget) **and** the `@@ -1348 @@` hunk
  *reverses nominal-first rejection*: it changes
  `if dependency is None or type(dependency) is allowed_type: continue` to
  `if dependency is None: continue` so the scanner runs **before** the exact-type
  check. That is P1 of the handoff's four blocking findings. → **DELIBERATELY
  RETIRED, do not replay** (see the pin check below).
- hunk `@@ -1393,6 +1414,297 @@` — **+274/-0** — `_SessionInterpretationResolutionPlan`,
  `_SessionInterpretationResolutionPlanner`, `_SessionInterpretationResolutionValidator`.
  0 hits on PLAT and MAIN. → unique, part of the dropped bullet.

**4. `src/elspeth/web/sessions/protocol.py` — UNIQUE_AND_WANTED.** Adds the five
`SessionInterpretationResolution*` dataclasses, `resolve_pending` on
`SessionOperationInterpretationMutations`, `session_operation_context` on
`SessionServiceProtocol.resolve_interpretation_event`, and swaps
`create_or_reconcile_pending`'s `validator` → `validation_inputs`. Side-by-side
of PLAT:3027-3055 vs WIP:3014-3047 is quoted above.

**5. `src/elspeth/web/sessions/routes/interpretation.py` — UNIQUE_AND_WANTED
(step-8 item 3).** The hunk does three things at once, not one: (i) wraps the
endpoint in the compose lock + `SessionOperationLease.acquire(... COMPOSE ...)`,
(ii) moves the two F-19 reads inside that lease, (iii) assembles
`committed_response` before an `except Exception as cleanup_error:` that
structured-logs and re-raises only when nothing was committed. Rule 4
(BaseException still propagates) is satisfied by the `except Exception` width —
that is the invisible property step 8 warns about; it is visible here as the
*absence* of `BaseException`. Coupling: the same hunk passes
`session_operation_context=compose_operation_lease.context`, so it cannot be
taken standalone against PLAT's signature.

**6. `src/elspeth/web/sessions/service.py` — UNIQUE_AND_WANTED.** The WIP deletes
`SessionServiceImpl.resolve_interpretation_event`'s 363-line inline `_sync` DML
body (`@@ -7028,363 +6993,44 @@`) and replaces it with an exact-type
`SessionOperationContext` check + `FenceLossReason.TOKEN_MISMATCH` + a
`transaction.interpretations.resolve_pending(command, validator)` delegation.
PLAT still carries the original at `service.py:7381` with **no**
`session_operation_context` parameter and the inline `_sync` at `:7447`. Also
deletes `SessionServiceImpl._validate_patched_composition_state`.

**7, 8, 10. Integration/unit caller threading — UNIQUE_AND_WANTED (mechanical).**
Each wraps an existing `resolve_interpretation_event` call in
`SessionOperationLease.acquire(... COMPOSE ...)` and passes
`session_operation_context=`. Nothing else. Required by path 6, worthless
without it.

**9. `tests/unit/architecture/test_session_db_mutation_authority.py` —
UNIQUE_NEEDS_RULING.** Substantively adds one authority
(`InterpretationResolutionMutationAuthority` on `composition_states`,
`interpretation_events` update, `sessions` update) and one `AuthoritySymbol`
row for `_RepositoryInterpretationMutations.resolve_pending`. The **bulk of the
+104 is stale `line=` re-pins** on `_REVIEWED_WRITERS` rows (`line=2340→2320`
etc.) measured against the WIP tree; they are meaningless against PLAT and must
be re-derived, not copied. Note this file does not exist on MAIN.

**11. `tests/unit/web/composer/test_request_interpretation_review_tool.py` —
UNIQUE_NEEDS_RULING.** Threading, but it does it by **monkeypatching bound
methods onto a real `SessionServiceImpl`** (`instance.resolve_interpretation_event =
_resolve_pending_with_test_lease  # type: ignore[method-assign]`). That is both
a lint suppression and a masquerade site; AGENTS.md says whole-tree AST gates
pin the exact masquerade set, tests included. Harvest only after deciding
whether to re-express it as a fixture.

**12. `tests/unit/web/sessions/test_interpretation_events_routes.py` —
UNIQUE_AND_WANTED.** The two tests here **are the RED/GREEN proof for step-8
item 3** and are not named by step 8:
`test_precommit_resolution_failure_remains_primary_when_release_also_fails`
(rule 1) and
`test_committed_resolution_logs_release_failure_without_masking_receipt_or_mutating_twice`
(rules 2+3). Both grep 0 on PLAT.

**13. `tests/unit/web/sessions/test_interpretation_events_service.py` —
UNIQUE_AND_WANTED.** +601/-273: twelve `test_resolution_*` tests covering
rollback, version non-consumption, CAS zero-row, trigger mapping, cross-session
and wrong-kind contexts. All grep 0 on PLAT. This is the behavioural cover for
paths 1/3/4/6.

**14. `tests/unit/web/sessions/test_interpretation_validation_inputs.py` —
ABSENT_FROM_BOTH.** New file, 160 lines, 3 tests. `git cat-file -e` fails on
PLAT and MAIN. **Step-8 item 2 premise holds.**

**15. `tests/unit/web/sessions/test_operation_fence_wiring.py` —
UNIQUE_NEEDS_RULING — direct collision with a live platform pin.** The WIP
*deletes* `test_pending_interpretation_validator_rejects_nested_engine_handle`
and replaces it with
`test_interpretation_planners_use_closed_validation_inputs_without_graph_scanners_or_callbacks`,
whose body asserts:

```python
assert "_forbidden_validation_dependency" not in policy_source
assert "get_referents" not in policy_source
assert "SessionPendingInterpretationValidator" not in policy_source
assert "SessionInterpretationResolutionValidator" not in policy_source
```

The first two agree with the platform's retirement. The third **contradicts**
PLAT's live `test_pending_interpretation_validator_rejects_any_non_exact_catalog_carrier`,
which imports `_SessionPendingInterpretationValidator` from that very module
(PLAT:210) and asserts the exact-type refusal (PLAT:223). It also requires both
`create_or_reconcile_pending` **and** `resolve_pending` to take
`validation_inputs: SessionInterpretationValidationInputs` — i.e. harvest item
1's own closure proof depends on the dropped repository bullet. The file also
adds four other unique tests and a `resolve_pending` arm to
`test_fenced_unit_of_work_exposes_only_exact_composed_capabilities`.

**16. `tests/unit/web/sessions/test_static_direct_writers.py` — UNIQUE_AND_WANTED
(half already present).** Two `ReviewedWriter` rows added. The
`_RepositoryInterpretationMutations.create_or_reconcile_pending` /
`composition_states` row is **ALREADY_PRESENT** on PLAT at
`test_static_direct_writers.py:914-925` (row starts at :914) (different purpose prose, checkpoint
`bc9cb0d5d`). The `_RepositoryInterpretationMutations.resolve_pending` row is
unique — `git grep -c "_RepositoryInterpretationMutations"` on PLAT's copy of
this file is **1**.

---

## Trap 1 — the retired scanner and its pin: VERIFIED, not assumed

`git show a2176dfe2…:tests/unit/web/sessions/test_operation_fence_wiring.py`,
line **234**, verbatim:

```python
    assert not hasattr(__import__("elspeth.web.sessions.pending_interpretation", fromlist=["x"]), "_forbidden_validation_dependency")
```

It sits at the end of `test_pending_interpretation_validator_rejects_any_non_exact_catalog_carrier`
(defined at :206), whose `pytest.raises(TypeError, match=r"catalog must be the
exact process-local CatalogServiceImpl")` at :223 is the nominal-first refusal.
`git grep -w -n "_forbidden_validation_dependency" PLAT -- src tests` returns
**exactly that one line** — the symbol is gone from `src/` entirely. `c4ff80553`
("retire the validation-dependency object-graph scanner …", 2026-08-30,
-76 lines from `pending_interpretation.py`) is the commit that removed it.
`get_referents` greps 0 on PLAT.

**Ruling stands:** the four scanner hunks in path 3 are
UNIQUE_BUT_DELIBERATELY_RETIRED and must not be replayed. The `@@ -1348 @@` hunk
is worse than "retired" — it would re-open the P1 traverse-before-reject defect
the pin at :223 exists to prevent.

Internal inconsistency worth recording: the same WIP commit both *extends* the
scanner (path 3) and *adds a test asserting the scanner's name is absent from
the module* (path 15). The WIP is mid-edit; do not treat either half as its
author's settled intent.

---

## Trap 2 — comment 8915's "12 interpretation helpers": PARTIALLY TRUE

Comment 8915 (`elspeth-4d6c0dd0f5`, author `claude-deferred-platform-recovery`,
2026-08-30T11:36:19Z) says, of the merge `0b676d195` of LANE `4c59c9d02` onto
HEADSIDE `7cd2fc6db`: *"the 12 interpretation helpers the lane moved to
`sessions/pending_interpretation.py` now carry HEAD's versions"*.

Measured by AST (`u3/helpers.py`: parse each blob, extract top-level
`FunctionDef`/`AsyncFunctionDef`/`ClassDef` source, compare dedented
non-blank-line-normalised text):

- LANE `pending_interpretation.py` has **26** top-level symbols.
- **20** of them are also top-level in HEADSIDE `service.py` — i.e. 20 symbols
  the lane moved out of `service.py` while HEAD kept them there.
- **0** of the 20 remain in LANE `service.py` (the move is complete).
- In the merge result `MERGE:pending_interpretation.py`, **all 20 of 20** are
  byte-identical (normalised) to **HEADSIDE's** `service.py` versions.
- Of those 20, **11** were already identical lane-vs-HEAD (so "HEAD won" is
  vacuous for them) and **9** genuinely differed and took HEAD's text:
  `_matching_pending_requirement_index`, `_patch_structured_interpretation_prompt`,
  `_require_mapping`, `_resolve_invented_source`, `_resolve_model_choice_review`,
  `_resolve_pipeline_decision_review`, `_resolve_vague_term`,
  `_reviewed_content_identity`, `_surfacing_prompt_structure_hash`.

The twenty checked, in full:
`_InterpretationHashDomainV2Payload`, `_find_interpretation_review_node`,
`_find_llm_transform_node`, `_find_node_spec_from_state_record`,
`_interpretation_hash_domain_v2`, `_matching_pending_requirement_index`,
`_node_specs_from_state_record`, `_patch_llm_transform_prompt`,
`_patch_structured_interpretation_prompt`,
`_pipeline_decision_artifact_hash_from_state_record`, `_require_mapping`,
`_resolve_invented_source`, `_resolve_model_choice_review`,
`_resolve_pipeline_decision_review`, `_resolve_prompt_template_review`,
`_resolve_vague_term`, `_review_requirement_identity`,
`_reviewed_content_identity`, `_surfacing_prompt_structure_hash`,
`_validate_pipeline_decision_semantics_from_state_record`.
(The other 6 lane symbols have no HEAD `service.py` counterpart:
`_PreparedPendingInterpretation`, `_SessionPendingInterpretationPlanner`,
`_SessionPendingInterpretationValidator`, `_forbidden_validation_dependency`,
`_pending_interpretation_validation_candidate_digest`,
`_validate_patched_composition_state_for_policy`.)

**Verdict: the substance is TRUE and now measured — every moved helper carries
HEAD's version. The number "12" is not reproducible; I count 20 moved helpers
(9 of them contested). Drift since: `_patch_structured_interpretation_prompt`
no longer equals HEAD's version at PLAT, having been changed by post-merge
commits.**

**And 8915 is not evidence about `3f3857d20` at all.** It describes the merge of
LANE `4c59c9d02`. `git merge-base --is-ancestor 64b7d144e 4c59c9d02` → yes, and
`git rev-list --count 64b7d144e..4c59c9d02` → **12**: the lane is 12 commits
*past* the WIP's parent, and the WIP (2026-08-06) was cut from `64b7d144e`
directly, not from the lane tip. `git grep -w -l "resolve_pending" 4c59c9d02 --
src tests` returns nothing. So none of the WIP's resolution work was ever on the
lane and none of it could have been absorbed by that merge. 8915 was, in fact,
already telling us this — it ends its 1861 section with: *"the object-graph
scanner [was] NOT restored; note the lane's pending_interpretation.py still
carries `_forbidden_validation_dependency` (the scanner the handoff says to
replace with the closed DTO from the BROKEN WIP 3f3857d20) — left as-is"*.

---

## What step 8 should say instead

Take the three named items **plus** (in dependency order):

1. `protocol.py` — the five `SessionInterpretationResolution*` types, the
   `resolve_pending` protocol method, `session_operation_context` on
   `SessionServiceProtocol.resolve_interpretation_event`, and the
   `validator` → `validation_inputs` swap on `create_or_reconcile_pending`.
2. `coordination/repository.py` — `_RepositoryInterpretationMutations.resolve_pending`.
3. `pending_interpretation.py` — **only** the `@@ -1393,6 +1414,297 @@` hunk
   (`_SessionInterpretationResolution{Plan,Planner,Validator}`). Discard the four
   scanner hunks at `@@ -1239/-1258/-1275/-1348 @@`.
4. `service.py` — the fence check plus the delegation; drop the inline `_sync`.
5. Its proof: `test_interpretation_events_routes.py` (2 tests, item 3's RED/GREEN),
   `test_interpretation_events_service.py` (12 tests),
   `test_operation_fence_wiring.py` (4 of the 5 new tests — the fifth needs the
   ruling at path 15), and the caller threading in paths 7/8/10/11.
6. `test_session_db_mutation_authority.py` and `test_static_direct_writers.py`
   authority rows — **re-derived** against PLAT, never copied (stale `line=`
   pins; one ReviewedWriter row already exists on PLAT).

Or, if the operator prefers, rule the repository-authority move **out** of scope
and re-scope items 1 and 3 to work against PLAT's existing `validator` protocol
and PLAT's context-free `resolve_interpretation_event`. That is a real option,
but it is a *decision*, and step 8 currently makes it silently by omission while
still demanding the DTO's closure proof — which cannot be written without the
move. Either way, U3 must be answered before step 8 runs.

---

## Addendum — the two harvest files' recorded hashes: VERIFIED

```
$ git show 3f3857d20:src/elspeth/web/sessions/interpretation_validation.py | sha256sum
da8a9b85b43876dd1516e75719a31c7910cf6a1580163ca4821c7d21b81c6e0e   (blob 23f9e1ffe, 355 lines)
$ git show 3f3857d20:tests/unit/web/sessions/test_interpretation_validation_inputs.py | sha256sum
8e06c695f0d133066d7dd7ff228a246e8ea1d7b5b4eb90eda7e207e61292dfb2   (blob d7571fbf9, 160 lines)
```

- Step 8's claim that the test file "hashes exactly to the recorded
  `8e06c695…`" is **TRUE**, byte-for-byte, and the "160 lines" is exact.
- `interpretation_validation.py` hashes to **`da8a9b85…`**, i.e. the
  **2026-08-06 rescue** value, *not* the 2026-08-02 capture `6a8ef93a…` the
  handoff records at `handoff.md:190-191`. The "355 lines" is exact. This does
  not answer U2 (the *direction* and *size* of the drift between the two is
  still unmeasurable — the `6a8ef93a…` blob is not in the repo), but it does
  pin which side is in hand: **`3f3857d20` carries the later, 08-06 side**, so
  a resumer reviewing "the 355-line file on its merits" is reviewing the rescue,
  not the capture.

## Note on the `unique_lines` column

`unique_lines` in the per-path table is: whitespace-normalised, blank-stripped,
de-duplicated **added** lines from `git diff -U0 64b7d144e 3f3857d20 -- <path>`
that appear nowhere in the concatenation of `a2176dfe2`'s versions of all 16
slice paths. It is a text-set floor that tolerates cross-file moves; it is **not**
a semantic diff count and no classification in this document rests on it. The
classifications rest on `git cat-file -e`, `git grep -w -l <symbol>` over
`-- src tests`, and side-by-side reads of the named hunks.
