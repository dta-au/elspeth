# runtime report: U-38 → U-35b → U-37 → U-39 (0.8.10 Continuation runtime)

> **RESUME TASK 0 (hung tests) is the release's FIRST priority, owned by u45-firstcompose plus one coordinator-assigned owner. It is NOT this lane's job.** See `.claude/lanes/release-0.8.10/RESUME-FIRST.md`. This lane resumes its own unit, under the guardrails: one pytest at a time, `-n 3` max, `timeout 2400`, both hang files `--ignore`d, no directory runs without a slot. Do not touch the fencing gate, the reserve-contract tests or the pytest config. Maintainer's order, 2026-10-25.

> **Coordinator corrections (10-25, from COLD-START-REVIEW.md). These override the text below:**
> - The 273-file attribution list is in `.claude/worktrees/runtime-20261025/.claude/lanes/runtime/` (gitignored). If it is missing, re-derive it as the test files that import the changed modules.


Spec: `docs/architecture/design-notes/0.8.10-continuation-runtime-design.md`
(on the target) and the grace spec. Work branch `codex/0.8.10-runtime` in
`.claude/worktrees/runtime-20261025`. Lane logs:
`.claude/worktrees/runtime-20261025/.claude/lanes/runtime/` (written `$L` below).

## U-38: production continuation policy from settings — HANDED OVER

**Tip to land: `60953a830` on `codex/0.8.10-runtime-u38-final`** = target
`dcb742ecc` + one cherry-picked commit (`-x`). It is the same change as the
verified `d22419b28` (on `ba4b25ee2`) and `f135a2b04` (on `582cc93c2`). No U-38
file is touched by the target delta in either step (`git diff --name-only`
overlap empty both times, `$L/target-delta*.txt`).

History hygiene: the three U-38 commits, one of them the agent-stop WIP
preservation commit (8c201933e, U-38 files only plus an early duplicate test),
are folded into one commit with a real message. The tree is byte-identical to
`bd0277f41` before the masquerade fix (tree `f1f7a3aa0`). Pins:
`preserve/0.8.10-runtime-u38-unfolded-bd0277f41`,
`preserve/0.8.10-runtime-u38-land-ea96096e1`.

### What it does

- Six `composer_continuation_*` WebSettings fields plus
  `web/continuation_settings.py`; `continuation_policy_from_settings` returns
  None exactly when the flag is off. It is wired at `web/app.py`
  `ComposerAsyncOperationAuthority(continuation_policy=…)`.
- The persisted policy is versioned (`composer.continuation.policy.v1`); the
  decoder refuses a missing or unknown version, never defaulting.
- Defaults: nominal 420 (the design's 300 could never admit the first
  PRIMARY: the startup chain check refuses
  `nominal < primary + reserve + title + 3 × session_sql`), watermark 120,
  quantum 180, reserve 25, session_sql retention 5.0 (U-45 §13). The advisor
  physical bound maps from `composer_advisor_timeout_seconds`; guard =
  prompt-guard timeout + 5 s.
- The fixture is the production policy (U-45 §13.5).

### Review

Codex Astra (`$L/astra-u38.md`, brief `astra-u38-brief.txt`, exit 0): 7
findings. 6 were adopted after source verification: the first-segment chain
(420 s), representable bounds, decode tests asserting their specific refusal
with matching hashes, golden bytes pinned, the admission-binding lifecycle
test, and two stale tests migrated plus a duplicate removed. 1 declined:
duplicate JSON keys in `settings_from_env`, pre-existing loader behaviour
shared by every JSON setting. Not adopted: a recursion-depth note on the policy
decoder, which decodes only our own persisted bytes.

### Mutation controls (`$L/u38-mutations.log`, `$L/u38-mutations-2.log`)

M1–M15 all go RED: default-on flag, quantum literal, advisor physical, guard
margin, defaulted version, fixture ignoring settings, app not wired, watermark
validator, env registry, chain validator, segment and stage upper bounds,
start using the current nominal, unknown version accepted, field order.

### Gates and attribution (same load on both sides, concurrent, `-n 3`)

`$L/tools/attrib_run.sh` on the U-38 tree `bd0277f41` and on base `582cc93c2`:
`frozen=YES` on both (`$L/u38-attrib.exit`, `$L/base-attrib.exit`).

**Exclusion:** `tests/unit/architecture/test_web_landscape_mutation_fencing.py`
was not run on either side (AGENT-BRIEF § KNOWN HANG). The first attempt, with
only the escape walk deselected, stalled on both sides and was stopped.
Fencing evidence instead: the sha256 of each of the 12 reviewed full-source
guarded files against `reviewed-full-source-guards.json`. On the U-38 landing
tree and on base, the only mismatch is `sessions/service.py`, the pending F-1
correction; U-38 adds none. Positive control: the U-35b tree flags exactly its
three edited guarded files.

| Selection | U-38 | base | Red on U-38 only | Red on base only |
| --- | --- | --- | --- | --- |
| Sessions gate + masquerade gate | 3 failed, 539 passed, 1 xfailed | 2 failed, 540 passed, 1 xfailed | `test_masquerade_gate.py::test_live_tree_has_zero_unbaselined_findings` (the getattr probes; **fixed** in `d22419b28`, see below) | — |
| `test_continuation_composition_initialization.py` | 3 failed, 23 passed | 20 failed, 6 passed | — | 17 (retention `TimeoutError` at 1.0 s) |
| `scripts.check_contracts` | exit 0 | exit 0 | — | — |

Red on both, attributed:

| id | base | U-38 | cause |
| --- | --- | --- | --- |
| `test_session_db_mutation_authority.py::test_composer_preferences_facets_are_exact_contained_and_bidirectional` | red | red | pre-existing on the target (`assert ([WriterIdentity…]) == ([], [])`) |
| `test_session_db_mutation_authority.py::test_receipt_bound_state_revert_and_fork_settlement_have_exact_authorities` | red | red | pre-existing on the target |
| `test_continuation_composition_initialization.py::test_constructed_first_compose_enters_original_guarded_primary_once[True]` | red (timeout masks it) | red | U-45 §10.4 quota double seed, unmasked; U-45's fix |
| `test_continuation_composition_initialization.py::test_initial_capture_retains_actual_reader_and_rechecks_historical_blob_access` | red (timeout masks it) | red | U-45 §10.1 historical blob text, unmasked; U-45's fix |
| `test_continuation_composition_initialization.py::test_paid_ready_and_first_compose_replay_preserve_real_later_rename` | red | red | retention `TimeoutError` under load ~40; 5.0 s needs U-45's §8 fix too (§13) |

### Masquerade fix and final-tree checks

The masquerade gate found `getattr` probes in the new test file: 5 occurrences,
with nested calls at line 91. All were replaced by `model_dump()` lookups; there
are now 0 `getattr` in the file (`git show <tip>:… | grep -c getattr` = 0, was
5).

- Landing tree `d22419b28`: masquerade gate + policy tests, 299 passed
  (`$L/u38land-masq.log`).
- Keyless set-diff `d22419b28` vs its base `ba4b25ee2`: **0 added, 0 removed**
  (3302 / 3302; `$L/targetba4.norm`, `$L/u38land.norm`). Before the fix it was
  +4, all masquerade.attribute-probes in this file.
- Final tree `60953a830` (on target `dcb742ecc`): masquerade gate + U-38 test
  files (`test_continuation_policy_settings`, `test_continuation_policy`,
  `test_continuation_policy_binding`, `test_composer_continuation_claim_start`,
  `test_config`): **766 passed, 5 skipped, exit 0** (`$L/u38final.log`).
- Landing rule (target AGENTS.md § Test Verification Policy): no id is red on
  U-38 and green on base once the masquerade fix is in. Every id U-38
  introduces passes at the commit. The red-first tests are folded into the one
  commit.

## Attachment-codec fixture (landed by the coordinator as `ba4b25ee2`)

Root cause: the `stored_evidence` fixture held an opaque `composer.messages.v1`
attachment chunk, while evidence restore reproves attachment history through
the real decoder. Over the 8 consumer files: base 56 failed / 23 passed, fix 2
failed / 77 passed. Of the 2 at `-n 0`: `test_actual_application_rolls_back_…`
passes both parameters; `test_next_actual_primary_extends_real_applied_p4_history_and_cold_current_graph`
still fails with the 1.0 s preparation `TimeoutError` of the base tree's old
literal fixture at load 41 (`fix-two-serial.log`).

My earlier `base-coord.log` was not a valid baseline. It collected 928 tests,
where the identical command at `60b5b27b9` now collects 1,185 and fails these
13 (`$L/probe60b-coord.log`).

## U-35b — in progress (see below when handed over)

**State at `42d2d7e56`** (`codex/0.8.10-runtime-u35b`, rebased onto `d7ad18596`;
`codex/0.8.10-runtime` points at the same chain; preserve refs
`preserve/0.8.10-runtime-u35b-674026773`, `preserve/0.8.10-runtime-pre-u35b-split`).

| check | base `d7ad18596` | tip `42d2d7e56` | note |
| --- | --- | --- | --- |
| Sessions gate file, -n 4 | 2 failed, 296 passed, 1 xfailed | 2 failed, 297 passed, 1 xfailed | identical (id, signature) pairs: the service.py preference and receipt facets; approved xfail unchanged |
| keyless set-diff | — | +2, −0 | both R5 `isinstance(outcome, ComposerOperationError)`, the closed owned union also used at the allowlisted V1 sites; left for adjudication |
| transitive recipe snapshot | ≠ manifest | byte-identical to base | pre-existing drift |
| writer recipes | 3 failures | same 3 | audit_storage, provider_quota; a planted lease-release mutation adds a lifecycle row (control) |
| PG handover/yield re-export | — | 18/18 passed, -n 0 | `tests/testcontainer/web/test_composer_continuation_handover_postgres.py` |
| attachment handoff ×4, serial | — | 3 pass; `live_handover-after` fails on SQLite and PG (lease_expired) | passes with this test's leases at 120 s (129 s body): lease time against work duration; awaiting design-protocol's lease sizing |

Bindings (rule R-G), all re-derived with the gate scanner
(`$L/tools/rederive_session_writers.py`, logs `$L/rederive-*.log`): the 9
`sessions/continuation.py` bodies (U-34, U-35a and U-35b) and
`_request_continuation_cancel_on_connection` bound to
`ComposerAsyncOperationAuthority`; `_release_fence_on_connection` under the
repository's existing binding. Mutation controls G1–G4 red. Digest re-pins: the
composer quota caller (`admit` changed only in its D-2 replay argument;
instrument reproduces the old pin on base) and three reviewed full-source guards
(authority, lifecycle, protocol). Open for review: `consume_continuation_handoff`
is a new site under the existing `prepare_lease_release` ticket, which only the
reserve-proof walk (known hang) inspects. `service.py`'s guard was already
stale on base and is untouched.

Mutation controls: Y1–Y6, C1–C5, F1–F3 all red (two initial survivors fixed:
Y4 now replays the prior receipt at the operative fence lock; F3 led to a new
settled-event payload assertion). Astra review running
(`$L/astra-u35b.md`). Attribution over the 273 affected test files awaits a
wide slot.

## Decode cost of one continuation evidence read (lead request 10-25 05:52)

**Reproduced on `d7ad18596`** (frozen detached worktree `chk-rt-d7ad18596`, both
source roots on `PYTHONPATH`, `elspeth.__file__` verified). Instrument: a pytest
plugin that runs cProfile only inside
`ComposerAsyncOperationAuthority.read_continuation_turn_evidence`
(`$L/tools/readprof_plugin.py`; per-read `.prof` files and summary at
`$L/readprof-p4.*`). Host load 75.

| read | test | profiled wall | link checks | graph decodes | journal admissions | evidence decodes | `canonical_json` | credential scans |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| positive | `test_continuation_p4_application_restore.py::test_actual_cold_application_finishes_both_tickets_and_restores_the_shared_graph` | 23.35 s (≈5 s is event-loop poll) | 5 | 12 | 5 | 2 | 12,377 (13.6 s cum) | 11,711 |
| control ×2 | `test_continuation_evidence_restore.py::test_actual_current_read_restores_full_graph_without_writes` | 0.27 s / 0.29 s | 3 | 0 | 0 | 1 | 75 | 11 |

The positive test fails with `SessionOperationFenceLost: lease_expired`, the
symptom design-protocol reported.

**Where the work goes (one read).** The same immutable snapshot is admitted
again by every consumer that needs its graph. `require_evidence_application_links`
is used as the accessor that yields the `ApplicationGraph`, and each call
re-decodes `graph_json` and re-runs `admit_journal_values`, which decodes the
same `entry.graph_json` a second time (`continuation_evidence_application_data.py:258`
and `:534`). Within one read it runs from: `decode_turn_evidence` (via its
round-trip `encode_turn_evidence`, `continuation_evidence.py:366, :397`),
`require_selected_active_batch_graph`, `require_attachment_applications_on_connection`,
then `require_p4_application_journal_on_connection`, which decodes the evidence
chunk again and runs `require_selected_applied_batch_chunk` (`:704`). About 17
call sites in 8 modules use the accessor this way.

**Does U-45 part 2 cover it? No.** Part 2 (diagnosis §8.2, F1–F4) memoizes
`decode_continuation_snapshot`, `verify_private_chunk`, catalog schemas and
credential scans, scoped to `clock.run("retention")`. It does not memoize
`decode_application_graph`, the link check or the journal admission, and the
evidence read is the `CONTINUATION_READ_SQL` ticket of `restore_turn_evidence`,
not a retention stage. `restore_turn_evidence` has no production caller on
`d7ad18596` (grep: only its definition); its first caller is U-37's driver.

**Proposed fix (at the producer, no cache).**
1. Admit once and carry the result. `decode_turn_evidence` and
   `capture_turn_evidence` return a frozen owned `AdmittedTurnEvidence`
   (snapshot plus its admitted `ApplicationGraph | None`), constructed only
   after the complete admission. Consumers that today call the link check to
   get the graph read the admitted graph instead. Their own SQL and
   cross-row comparisons stay as they are; only the pure re-admission goes.
2. Decode the journal graph once per admission: `admit_journal_values` takes the
   graph the link check already decoded instead of decoding `entry.graph_json`
   again.
3. `require_p4_application_journal_on_connection` receives the admitted evidence
   from `read_selected_turn_evidence_graph_on_connection` instead of re-decoding
   the same chunk. Its SQL reads stay.
Expected effect (I, not measured): link checks per read 5 to 1, graph decodes
per check 2 to 1. Nothing is memoized, so there is no scope, key or invalidation
to defend, and the G1–G8 guarantees are untouched.

### U-35b after the Astra review (tip `e77a20c70`)

Astra (`$L/astra-u35b.md`) requested changes. Every finding was verified against
source and fixed:

| finding | fix | commit | controls |
| --- | --- | --- | --- |
| P1 handoff sealed required work before commit; a Stop/CAS refusal could not settle or retry | new drain-admissible `RequiredWorkSource.CONTINUATION_HANDOFF_SQL` (one recurrence per attempt), barrier sealed only after commit; a no-write refusal restarts renewal and un-retains the finish task | `7074ce4b8` | P1a–P1f red |
| P3 consumed lease never `closed` | `_closed` set for committed/lost/unknown | `7074ce4b8` | P1e red |
| P2 terminal ignored supplied claim token/attempt; read-back compared only the fence id | exact claim/attempt and full attempt binding | `8a59423a9` | P2a–P2e red |
| P2 cut coverage bypassed the production selector | every cut and in-flight shape also through `next_continuation_action_policy` with a real closed chunk cohort | `21315730a` | P4a–P4b red |
| P3 renewal test accepted no renewal | strict extension from a short live claim | `9d6386ffd` | P5 red |
| P3 R8 covered one sequence | saves folded across two yielded sequences | `9d6386ffd` | Y19 red |

Astra cleared both remaining keyless R5 `isinstance` findings as correct
owned-union discrimination (ADR-032), so the keyless +2 is adjudicated.
Guarded-file cost of P1: `required_work.py` and `lifecycle.py` re-pinned
(`a8c43f54e`); the transitive recipe snapshot differs from base only in
`RequiredWorkSource` and `SOURCE_MAPPING`; writer recipes unchanged.

Design §6.1 cases that were missing and are now present (`9d6386ffd`,
`e32dc4d2a`, `573016daa`, `bec4e5a28`): lease consume (no test before), stale
owner per element with a targeted body witness, hard-deadline admission,
unsafe cut (incl. a dispatched effect row), rollback after each of steps 5–11,
read-back distinctions, recovered-successor read-back, successor runtime read,
CAS retry under one lease, settle-lost pairing, every landed owner (pre-title,
bootstrap, first-Compose runtime, P4, attachment) reading a live handover as a
recovery, a two-process yield/claim race, and yield/handover latency. Mutation
controls Y7–Y19, SL1–SL2, OW, PR1–PR2 red. The lineage reader is U-40's by
agreement; the driver-side "yield refused where the owner does not read
handoff lineage" case is U-37's.

Verification at `e77a20c70` (frozen `chk-rt-e77a20c70`):

| check | result |
| --- | --- |
| Sessions gate file, -n 4 | 2 failed, 297 passed, 1 xfailed; the same 2 pre-existing service.py ids as base |
| keyless set-diff vs `d7ad18596` | +2 (the adjudicated R5), −0 |
| full-source guards | all 12 match except `service.py` (stale on base, untouched) |
| U-35b + touched files, -n 4 (744 ids) | 735 passed, 9 failed: `test_continuation_runtime.py` message-family producer ids, identical (id, signature) on base (`guarded/native message has no actual registered current producer`) |
| PostgreSQL, -n 0 | re-export 53/53 + race, latency and owner handover params 18/18 (PG yield p99 0.30 s, handover p99 0.31 s) |
| fencing_inventory.py | does not finish on either tree (known hang; killed after 13 min, as every earlier run of it here); guard digests are the fencing evidence |
| attachment `live_handover-after` | still red on SQLite and PG (lease time vs work); awaiting design-protocol's lease sizing |
| wide attribution (273 files) | awaiting a slot |

Landing note: the branch carries WIP commits whose intermediate trees have
gate-pin reds; land U-35b as one squashed commit once the lease sizing is in.

### Decode-cost unit: call sites (read-only map at `e77a20c70`)

The link check `require_evidence_application_links` is called at 17 sites and
`decode_turn_evidence` at 11; `admit_journal_values` runs inside the link check
and at three more sites. Owners by module: `composer/continuation_evidence*.py`,
`composer/continuation_attachment_*.py`, `composer/continuation_message_families.py`,
`composer/continuation_selected_batch_graph.py`, `composer/continuation_runtime.py`,
`sessions/continuation_attachment_application.py`,
`sessions/continuation_p4_application_store.py`,
`sessions/continuation_composition_initialization.py`,
`sessions/continuation_primary.py`. The admit-once change touches the producers
(decode/capture) and the consumers that only need the graph; consumers that
compare two different snapshots (`require_attachment_projection` before/after)
still admit each once. Not started: awaiting the lead's routing and line
coordination with design-protocol.

### U-35b landing commit: `a8aeab905` on `codex/0.8.10-runtime-u35b-land`

One squashed commit on `d7ad18596`, made with hooks in the worktree
`.claude/worktrees/runtime-u35b-land`. Its tree is byte-identical to the
working branch's tip `4c5e08d2d`. The commit message carries every
disclosure: bindings, digest re-pins, the new source and recipe drift, the
new required-work sites, the adjudicated R5 findings, and the fencing-tool
hang. `branch-safety-check.sh --intent merge --base d7ad18596` shows FAIL=0,
WARN=1. The warning is the shared rerere cache, and the squash used no
rerere merge.

Since `e77a20c70`:
- The attachment leases use design-protocol's recorded rule (`OWNER_LEASE_SECONDS`). All four handoff parameters pass serially on SQLite and on PostgreSQL.
- The full attachment file has 17 passing and 4 failing. The 4 failures are the `prior_real_p4_journal` parameters, with the same id and signature on base: `TypeError: cannot pickle 'mappingproxy' object`. That is the deepcopy defect addressed on design-protocol's branch.
- The refused-handoff renewal witness now uses a 6 s lease. Its control is still red.

Still owed: the wide attribution over the 273 affected files, which awaits a slot.

## COLD START (paused 2026-10-25), coordinator-written

*Written by the coordinator ("0.8.10 - Main"). design-runtime kept working through four pause notices and had just created `runtime-u37` to start U-37, so it was stopped with TaskStop. At that moment all 11 of its trees were clean and nothing was running (verified). Facts come from git at d7ad18596 and the lane's final messages. Re-verify before acting.*

**Ready to land (on resume, after Task 0 per RESUME-FIRST.md and after 506561cd4):**
- **U-35b = `a8aeab905`** on `codex/0.8.10-runtime-u35b-land`: ONE squashed commit on d7ad18596 (1/0). Its tree is byte-identical to the working tip `4c5e08d2d` on `codex/0.8.10-runtime-u35b` (22/0, WIP history). The commit message carries every disclosure: bindings, quota-caller + four guard re-pins, the new `CONTINUATION_HANDOFF_SQL` required-work source and its recipe drift, the new required-work sites (only the hanging walk inspects them), the adjudicated R5 pair, and the fencing-tool hang.
  - All 6 Astra findings are fixed (P1 barrier-after-commit, P2 exact claim/attempt, P4 production selector, P5 strict renewal, P6 real R8), and the §6.1 library tests are in.
  - The lease blocker is RESOLVED: design-protocol's rule (`b296259db`, `tests/helpers/continuation_budgets.py`, OWNER_LEASE_SECONDS=600, PRIMARY_ALLOWANCE_SECONDS=120.0) was applied, and the helper was brought in byte-identical (sha256 1cf04ef7), so protocol's landing adds the same file cleanly. live_handover-after passes on SQLite + PG.
  - test_continuation_attachment_application.py: 17P/4F. The 4 `prior_real_p4_journal` ids fail identically on d7ad18596 (`TypeError: cannot pickle 'mappingproxy'`), which is the ebf73a853 freeze defect fixed on design-protocol's codec-residue branch (3d91561d3).
  - PG 53/53 + 18/18; keyless +2 adjudicated by Astra; sessions gate = the 2 inherited reds, which 506561cd4 fixes.
- **Still owed before landing:** attribution over the 273 affected files, d7ad18596 vs a8aeab905. Run them one after the other at `-n 3` under `timeout`, both hang files ignored, unless the coordinator grants a slot. Also rebase onto 506561cd4 and re-derive pins with the tools if the sessions pins move.

**Next units (in order):**
1. **U-38c decode cost** (admit-once AdmittedTurnEvidence, no cache), approved at 06:24 with conditions: per-site check accounting, before/after counts, a pinned decode-count test plus a negative control, and line coordination with design-protocol (whoever lands second re-applies; no reverts). It must come BEFORE any U-37 drain/SIGTERM test, because §6.2 drain tests would otherwise measure a 7–23 s evidence read against D = 10 s.
2. **U-37**: the pieces that need no evidence read can start once U-38c is underway: the two settings + grace validator, the uvicorn `handle_exit` override, the stop()-after-drain deadline fix, and the pure `classify_drain_cut`. It needs a third disclosed re-pin of required_work.py (outcome kinds). The worktree `runtime-u37` exists at a8aeab905, clean, with no work in it.
3. U-39, U-36 per the plan. Also, design-protocol's interim lifecycle-2 Stop refusal gets replaced by the real Stop after U-35b lands (OWED BEFORE THE MERGE).

**Also owned:** if the §9 idle-host run (coordinator) fails at p95 > 2.5 s at 50 pairs on PG, re-size the U-38 policy. PG p99 measured for U-35b: yield 0.30 s, handover 0.31 s.

**Scratch trees, clean and removable via worktree-cleanup.sh once merged:** chk-rt-*, runtime-60b-probe, runtime-base, runtime-target-ba4, runtime-u38-land, runtime-u38-verify, design-runtime-20261024.
