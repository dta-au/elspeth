# Park identity Task 2 implementation report

Worktree: `/workspace/elspeth/.claude/worktrees/composer-remediation-park-identity`.
Base and current HEAD: `05bcab93955b2070ffdc90e72eddd7368502d2c1`.
No commit, rebase, live provider request, dependency installation, migration, store reset, policy change, or active integration source edit was performed in this lane.

## Implementation and compatibility

The sole production change is four added lines in `src/elspeth/web/composer/graph_recovery.py`: import the Task 1 `canonical_resource_uuid` helper; after the existing restore selector type, raw length, and nonblank checks, project `park_id.strip()` through it; if parseable, assign the canonical UUID string using `object.__setattr__` during frozen construction.

This makes canonical, uppercase, mixed-case, braced, hyphenless, lowercase `urn:uuid:`, and padded valid park selectors identify the same owned intent. There is no UUID-version restriction. Non-UUID text, including padded non-UUID text and uppercase `URN:UUID:`, remains byte-for-byte unchanged and retains the unavailable-target path. Raw length limits still apply before stripping. Message and tool-call-ID whitespace are preserved.

`recovery_controls.py`, the message credential guard, strict argument models, sole-call batching, provider invocation, session-filtered lookup, target qualification, fences, settlement, pending-proposal dispositions, and persisted identities are unchanged. No loaded row or old proposal/receipt is normalized. The D15 refusal policy remains intact: unknown and foreign targets return the same `target_unavailable` result and preserve the head and pending proposal. Owned malformed graph metadata remains unavailable; noncanonical owned park row identities still fail admission.

The actual provider tests assert one provider invocation, advertised canonical park/state identities, route-specific palettes, both exposed dialects, original provider message, a new graph-only state derived from the exact parked state, `graph_recovery` provenance, `is_valid=False`, absent completion metadata, and private evidence retaining the original alias/message and provider tool-call identity with physical-call/operation binding. The restored target may not pass through `CompositionState.validate`; validation of the initial graph while building a normal prompt remains permitted.

Task 1's intentional canonical successful blob-ID echo correction remains inherited from the base; Task 2 adds no blob response/schema changes. Old canonical pending/applied proposal documents, argument hashes, and receipts are untouched by this patch. This lane does not repeat Task 1 receipt tests or claim new coverage for its crash-recovery contracts.

## Changed paths

1. `src/elspeth/web/composer/graph_recovery.py`: constructor identity normalization only.
2. `tests/fixtures/resource_identity.py`: seven park spellings and a case-bearing UUID fixture.
3. `tests/unit/web/composer/test_graph_recovery.py`: constructor/parser aliases, exact message/raw dictionary preservation, unchanged non-UUID and type/length/blank behavior, numeric-only seed fixture control.
4. `tests/unit/web/coordination/test_graph_recovery_persistence.py`: real park creation and later restore for all seven spellings; exact derivation/provenance assertions.
5. `tests/unit/web/sessions/test_composer_recovery_security.py`: owned/foreign/unknown selector matrix, unchanged pending-proposal refusal behavior, persisted metadata and park-identity corruption controls.
6. `tests/unit/web/sessions/test_graph_recovery_turn.py`: actual freeform/planner/malformed-head recovery matrix and private evidence; alias/foreign refusal cases retain the original received-tool filter.
7. `tests/testcontainer/web/test_composer_operations_postgres.py`: reuse the own/foreign/unknown preparation and pending-refusal proofs under the existing real PostgreSQL harness. The existing own-park roundtrip import automatically receives its new seven-spelling parametrization.

The fixture gives otherwise random UUIDs a guaranteed alphabetic first hex digit; it does not convert aliases into canonical no-op inputs. Production park creation is patched only for the duration of that test's `prepare_graph_recovery` call, with an autospecced UUID factory; all persistence, authority, and settlement are real. Numeric-only seeds `UUID(int=1)` and `UUID(int=2)` produce distinct identities and genuinely changed uppercase/mixed selectors that parse back to those identities.

## Evidence and provenance

Evidence directory: `.claude/lanes/composer-remediation-wave1/park-identity-evidence/` in the main checkout. Each runner has a separate full log and explicit `.exit` file. Both `elspeth.__file__` and `elspeth_lints.__file__` resolve into this implementation worktree in `provenance.txt` and `resume-provenance.txt`. Every pytest invocation exports both source roots and explicitly overrides `-o pythonpath`; all tests run with `-n 0`.

`AGENTS.md` and `CONTRIBUTING.md`, including whole-tree gate instructions, were read. No attribute pin was widened: the explicit frozen-construction idiom does not add a `getattr`, `hasattr`, `getattr_static`, or `__getattr__` probe. No wire model, declaration, field inventory, redaction snapshot, suppression, HMAC key, or signing metadata was edited.

The actual before scan is retained as `keyless-before.log` / `.exit` (exit 1), run with `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing`, `--rules all --root src/elspeth`, against base production source before this lane's production edit. Its original provenance is retained. **Baseline limitation:** the interrupted predecessor did not leave before/after source digests for that scan; this lane does not retroactively call it a frozen-source proof. The base revision and unchanged initial production status were checked on resume. The parent owns the combined final all-rules scan and baseline adjudication; this lane intentionally did not duplicate it.

`final-freeze-before.head`, `.diff.sha256`, and `.untracked` bind the final fixture-adjusted candidate. The final patch contains all seven tracked changed files and no untracked paths. Source verification after the final gates is recorded separately below.

## Measured runs

| Evidence prefix | Exit | Measured outcome / meaning |
| --- | --- | --- |
| `red` | 1 | Original predecessor run: 52 failed, 4 passed, 46.87s. Mixed diagnostic: aliases genuinely fail; four canonical provider cases also hit an overbroad test validation blocker. Do not describe all 52 as intended identity reds. |
| `red-provider-corrected` | 1 | Narrowed validation guard, production unchanged: canonical freeform succeeds; uppercase freeform reaches real settlement and fails with the unavailable-target notice. 1 failed / 1 passed, 5.58s. |
| `green-recovery` | 1 | Six focused recovery files: 201 passed, 14 failed, 116.42s. All 14 failures are new planner fixtures missing required provider cost metadata, including canonical cases; they do not establish a production regression. |
| `green-corrected-harness` | 1 | Correct planner cost fixture: 86 passed / 14 failed, 67.73s. All 14 planner cases pass restoration and persisted-state assertions, then fail an inaccurate test-only evidence-name filter. |
| `green-provider-final` | 2 | Interrupted, void run: 18 failures / KeyboardInterrupt, 23.21s. An ambiguous edit had touched the earlier refusal filter and left an undefined facts reference in the new test. The mistake was detected, the run interrupted, the earlier test restored, and the exact intended block inspected against base before retry. This run is not candidate validation. |
| `green-provider-frozen` | 0 | Corrected actual-provider matrix: 42 passed, 42.11s; all seven spellings × all three routes × both dialects. |
| `green-fixture-final` | 0 | After ensuring case-bearing IDs, only affected uppercase/mixed persistence, preparation, pending-refusal, provider-success/refusal cases plus numeric fixture control: 29 passed, 70 deselected, 27.37s. |
| `negative-control` | 1 | Isolated lane-copy mutant with normalization removed: 48 failed / 8 passed, 43.07s. See exact qualification below. |
| `postgres-identity` | 0 | Real serial PostgreSQL own-park roundtrip, owned/foreign/unknown preparation, pending-refusal matrix: 28 passed, 33.36s. |
| `ruff` | 1 | Initial test import ordering issues, corrected with Ruff's import sorter. |
| `ruff-final-freeze` | 0 | Ruff on all seven final touched files. |
| `ruff-format-final` | 0 | `ruff format --check` on all seven final touched files; 7 files already formatted. |
| `mypy` | 0 | `mypy --follow-imports=silent src/elspeth/web/composer/graph_recovery.py`; no issues in one source file. |
| `contracts` | 0 | `python -m scripts.check_contracts`; soft mapping census and all reported contract checks pass. |
| `trust-boundaries` | 1 | The three declaration rules retain exactly 205 existing occurrences / 204 distinct findings; Counter delta +0 / -0. |
| `docker-info` | 0 | Managed local Docker socket reports server 29.9.2 before PostgreSQL tests. |
| `affected-gates` | 0 | Exact seven-file selection below: 604 passed, 1 xfailed, 479.99s. The xfail is the Sessions mutation-authority inventory's reported standing unreviewed/stale classifications; its complete diagnostic is retained in the log. |

The final fixture adjustment changes test identity generation only; the production source is identical across all post-fix runs. No whole recovery rerun was performed for fixture-only edits. The complete initial six-file run's passed cohorts, the corrected 42-case provider run, final affected fixture cases, and narrow gates together cover the intended change; this is **not** a claim that a full default suite or full PostgreSQL selection passed.

### Exact pytest scope and invocation options

All runs use the implementation worktree as cwd, `.venv/bin/python -m pytest -n 0`, exported `PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"`, and `-o pythonpath="$PWD/src $PWD/elspeth-lints/src"`. Final affected/PG/mutant selections also use `-q --tb=short`. The negative control substitutes its lane-copy `src` for `$PWD/src` in both locations and records `--junitxml=.../negative-control.xml`.

The `green-recovery` selection is exactly:

```text
tests/unit/web/composer/test_graph_recovery.py
tests/unit/web/composer/test_planner_graph_recovery.py
tests/unit/web/composer/test_recovery_control_palettes.py
tests/unit/web/sessions/test_composer_recovery_security.py
tests/unit/web/sessions/test_graph_recovery_turn.py
tests/unit/web/coordination/test_graph_recovery_persistence.py
```

The `affected-gates` selection is exactly:

```text
tests/unit/web/composer/test_graph_recovery.py
tests/unit/web/test_sessions_composer_attribute_contracts.py
tests/unit/test_mock_discipline_baseline.py
tests/unit/architecture/test_session_db_mutation_authority.py
tests/unit/web/composer/test_tool_redaction_policy.py
tests/unit/web/composer/test_tool_result_envelope_gate.py
tests/unit/web/composer/test_compose_loop_envelope.py
```

The `postgres-identity` selection adds `-m testcontainer` and is exactly these three existing PostgreSQL harness nodes (7 + 7 + 14 parameter cases):

```text
tests/testcontainer/web/test_composer_operations_postgres.py::test_explicit_restore_uses_parked_identity_across_turns
tests/testcontainer/web/test_composer_operations_postgres.py::test_restore_other_session_park_matches_unknown_refusal_and_preserves_head
tests/testcontainer/web/test_composer_operations_postgres.py::test_completed_restore_refusal_preserves_head_and_pending_proposal
```

The `negative-control` selection is exactly the constructor alias test, SQLite roundtrip, and provider-route alias matrix:

```text
tests/unit/web/composer/test_graph_recovery.py::test_restore_intent_and_parser_canonicalize_only_external_park_selector
tests/unit/web/coordination/test_graph_recovery_persistence.py::test_explicit_restore_uses_parked_identity_across_turns
tests/unit/web/sessions/test_graph_recovery_turn.py::test_provider_park_alias_restores_exact_advertised_graph_and_retains_raw_evidence
```

## Mutation-control qualification

`negative-control/src` is a lane-local copy of source with `graph_recovery.py` replaced by its base version, removing exactly this production change. `negative-provenance.txt` proves imports from that copy and the implementation worktree's lints root. A tracked-source byte comparison confirms that only `src/elspeth/web/composer/graph_recovery.py` differs between mutant and final candidate.

`negative-control.xml` was checked programmatically, preserving each testcase outcome:

- Constructor: canonical 1 passed; six aliases failed normalization assertions.
- Real SQLite roundtrip: canonical 1 passed; six aliases failed exact target selection.
- Actual provider paths: six canonical controls passed (all three routes, both dialects); all 36 noncanonical cases failed with the actual unavailable-target recovery notice.

This qualifies the corrected planner cost/evidence harness independently: canonical planner restoration succeeds without the fix while equivalent planner aliases fail through real settlement. `negative-control-validation.txt` records these category counts and source-copy comparison.

## Static diagnostic comparison

`trust-boundaries.log` runs only `trust_boundary.tests,trust_boundary.scope,trust_boundary.tier` against `src/elspeth`, with both roots exported. The actual before-all-rules log and this log were compared as a **Counter** of `(path, rule, full message)`, excluding line/column offsets and `R_TB_SUPPRESSED` observations that are emitted only by the all-rules selection. Both contain 205 occurrences / 204 distinct findings; added and removed occurrence counts are zero.

The comparison instrument was controlled with an equal/line-offset pair, changed-message positive, duplicate-occurrence positive, and unrelated-rule/suppression negative. Full result: `trust-boundaries-comparison.txt`. It does not certify a zero-finding tree, operator signature clearance, or the parent's combined all-rules delta. No signing or suppression workaround was used.

## Adjacent observation and scope limits

Existing planner received-evidence categorization may label a recovery control `unknown_tool`: `pipeline_planner.py` passes `_WIRE_TOOL_DEFS[effective_dialect] | {_TERMINAL_TOOL_NAME}` to `capture_received_response` (around lines 3917–3922), while recovery controls are added separately to planner palettes. `private_diagnostic_facts.py` (around lines 217–273) retains the original `provider_tool_name`, `provider_tool_call_id`, and arguments while choosing that category. The new identity test uses those existing received facts. This is an adjacent observation for later audit triage, not a newly asserted defect or a production change in this lane.

Unclaimed: full default behavior suite, full PostgreSQL contention suite, CI long proofs, live provider/browser acceptance, whole-tree masquerade corpus, full mypy, final combined all-rules scan, pagination/BOS-01, parked-window paging, inspect-source snapshot/failure ownership, encoding-aware information keys, model/secret discovery, general R2/XC-13 closure, and umbrella audit-finding closure. The parent owns combined merge-policy verification and fresh independent Astra review.

## Handoff

Complete patch: `.claude/lanes/composer-remediation-wave1/park-identity-review.diff` in the main checkout. No untracked implementation files exist, so the patch includes the complete tracked candidate without requiring a separate new-file bundle. Leave this worktree frozen at the stated base for independent review; do not rebase it onto the parent's later public-lifecycle integration.

Initial reviewed-candidate verification is now terminal: `affected-gates.exit` is 0, with 604 passed / 1 xfailed. `final-freeze.txt` verifies exact before/after equality of HEAD, complete binary diff digest, and empty untracked inventory. Patch SHA-256: `1a9999a08b9983d0cfb7a88cc508f1f9fab3518226d5c8e1487f55579c7a8616`. Production file SHA-256: `a0bc235eb91690b063fcef6e9533df623d136fda032b5c9b37887289330a101c`.

Independent Astra review (`park-identity-review.md`) requests one P2 test-only correction: pin complete indistinguishable assistant and recovery-notice refusals and exclude canonical/raw selectors from both. No blocking production defect was found. The parent authorized a narrow fix round only after this terminal/frozen initial candidate was preserved. That fix will be documented below; this initial report does not claim review approval.

## Fix round 1 — complete public-refusal oracle

The resumed fix changes only the body of `test_refused_provider_recovery_retains_exact_private_control_and_physical_binding` in `tests/unit/web/sessions/test_graph_recovery_turn.py`, relative to the reviewed candidate. It replaces the three partial public-text assertions with independently written complete assistant and recovery-notice expectations. The same two expectations apply to unknown and foreign targets across all seven selector spellings and both malformed-head cases. Only the existing malformed-head notice suffix is conditional. Both public texts also explicitly exclude the prospective success message, canonical inaccessible ID, and original raw selector.

The real provider-control parsing, settlement, completed-operation assertion, unchanged-head inventory, private raw arguments, physical-call/operation bindings, and public evidence filtering remain in place. The separate pending-proposal preservation tests are unchanged. No production candidate code, route matrix, fixture, planner behavior, or PostgreSQL test changed in this fix round. No commit, provider request, installation, broad scan, or child agent was used. The already terminal 604-pass/1-existing-xfail source-gate cohort, 42-case provider success cohort, final 29-case fixture cohort, real PostgreSQL 28-case cohort, and original normalization-removal control were not rerun.

### Terminal results and diagnostic qualification

Each prefix below has its own full `.log` and explicit `.exit` in `park-identity-evidence/`; pytest controls additionally retain `.xml` results.

| Evidence prefix | Exit | Result |
| --- | --- | --- |
| `fix1-refusal-green` | 0 | Initial strengthened oracle: 28 passed, 26.03s. Before the formatting-only adjustment; not the final source digest. |
| `fix1-ruff` | 0 | Ruff check passed. |
| `fix1-format` | 1 | Ruff requested formatting of two literal expressions; corrected with `ruff format`. |
| `fix1-refusal-final` | 0 | Final formatted candidate: 28 passed, 27.53s. |
| `fix1-ruff-final` | 0 | Final test file: all checks passed. |
| `fix1-format-final` | 0 | Final test file: 1 file already formatted. |
| `fix1-mutant-old-oracle` | 1 | Invalid harness diagnostic: all four lane-file cases stopped before settlement with `Shared executor recovery callback absent`. An outside-`tests/` file did not inherit root autouse fixtures. This is not a refusal-oracle red. |
| `fix1-mutant-old-oracle-controlled` | 0 | After explicitly loading the existing root/unit/web conftest plugins for that lane-file copy: 4 passed, 24 deselected, 5.71s. The original oracle accepts the disclosure mutant. |
| `fix1-mutant-new-oracle` | 1 | Same four braced-selector target-kind × malformed-head cases: 4 failed, 24 deselected, 5.95s. Every failure is the new complete recovery-notice equality assertion; each diff exposes the injected braced selector. |
| `fix1-validation` | 0 | Programmatic JUnit outcome/category checks, identical old/new control case names, exact mutant source change, original artifact preservation, production byte preservation, delta reverse applicability, and final before/after candidate freeze all pass. |

The narrow mutant is the pre-existing lane copy `park-identity-evidence/fix1-refusal-mutant/src`, inspected on resume and found byte-identical to candidate tracked source before mutation. Its sole tracked-source difference is one line in `sessions/service.py`, immediately after the common refused recovery notice:

```python
notice += f" Requested selector: {{{recovery.intent.park_id}}}."
```

For the selected `braced` cases this publishes the exact raw selector supplied to the provider control. The same mutation preserves the old `did not complete` substring and leaves assistant text unchanged, so it specifically demonstrates the reviewed gap. `fix1-refusal-mutant.diff` records the mutation. `fix1-validate.py` verifies the entire tracked-source comparison, exact added line, unchanged mutant service digest across the two controlled runs, old-oracle byte equality with the preserved reviewed test, and all four failures at `assert response.recovery_notice.content == expected_notice`. All four parameter names also occur in the clean 28-pass final candidate cohort. The initial missing-fixture run is retained and explicitly excluded from this qualification.

### Exact fix-round commands

The following path variables only abbreviate the actual absolute paths and arguments used. Every invocation used the implementation worktree as cwd; the positive and mutant selections exported both source roots and supplied the same roots explicitly through `-o pythonpath`.

```bash
park_root=/workspace/elspeth/.claude/worktrees/composer-remediation-park-identity
park_lane=/workspace/elspeth/.claude/lanes/composer-remediation-wave1
park_evidence="$park_lane/park-identity-evidence"
park_mutant="$park_evidence/fix1-refusal-mutant"
park_node=tests/unit/web/sessions/test_graph_recovery_turn.py::test_refused_provider_recovery_retains_exact_private_control_and_physical_binding
cd "$park_root"
export PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"
.venv/bin/python -m pytest -n 0 -o pythonpath="$PWD/src $PWD/elspeth-lints/src" -q --tb=short "$park_node" --junitxml="$park_evidence/fix1-refusal-green.xml"
.venv/bin/python -m ruff check tests/unit/web/sessions/test_graph_recovery_turn.py
.venv/bin/python -m ruff format --check tests/unit/web/sessions/test_graph_recovery_turn.py
.venv/bin/python -m ruff format tests/unit/web/sessions/test_graph_recovery_turn.py
.venv/bin/python -m pytest -n 0 -o pythonpath="$PWD/src $PWD/elspeth-lints/src" -q --tb=short "$park_node" --junitxml="$park_evidence/fix1-refusal-final.xml"
.venv/bin/python -m ruff check tests/unit/web/sessions/test_graph_recovery_turn.py
.venv/bin/python -m ruff format --check tests/unit/web/sessions/test_graph_recovery_turn.py

export PYTHONPATH="$park_mutant/src:$PWD/elspeth-lints/src"
# Initial outside-tests old-oracle harness diagnostic (not qualifying):
.venv/bin/python -m pytest -n 0 -o pythonpath="$park_mutant/src $PWD/elspeth-lints/src" -q --tb=short "$park_evidence/fix1_refusal_old_oracle.py::test_refused_provider_recovery_retains_exact_private_control_and_physical_binding" -k braced --junitxml="$park_evidence/fix1-mutant-old-oracle.xml"
# Correct old-oracle harness with the existing fixture plugins explicitly loaded:
.venv/bin/python -m pytest -p tests.conftest -p tests.unit.conftest -p tests.unit.web.conftest -n 0 -o pythonpath="$park_mutant/src $PWD/elspeth-lints/src" -q --tb=short "$park_evidence/fix1_refusal_old_oracle.py::test_refused_provider_recovery_retains_exact_private_control_and_physical_binding" -k braced --junitxml="$park_evidence/fix1-mutant-old-oracle-controlled.xml"
.venv/bin/python -m pytest -n 0 -o pythonpath="$park_mutant/src $PWD/elspeth-lints/src" -q --tb=short "$park_node" -k braced --junitxml="$park_evidence/fix1-mutant-new-oracle.xml"
.venv/bin/python "$park_evidence/fix1-validate.py"
```

For each logged check the command was redirected directly to its corresponding `$park_evidence/<prefix>.log` with `2>&1`, then its actual terminal `$?` was written with `printf '%s\n' "$rc"` to `<prefix>.exit` before displaying the log. All focused pytest processes ran serially; no second pytest was launched before the preceding process had exited. The formatted candidate's static checks ran while its single focused pytest process was active and did not modify source.

### Provenance, final freeze, and review artifacts

`fix1-provenance.txt` proves `elspeth.__file__` is under this worktree's `src` and `elspeth_lints.__file__` under this worktree's `elspeth-lints/src`. `fix1-mutant-provenance.txt` proves `elspeth` and `web.sessions.service` load from the mutant `src`, with lints still from the implementation worktree. Both were checked with explicit path assertions before their test work.

`fix1-final-before.{head,diff.sha256,untracked}` and matching `fix1-final-after.*` bind the final formatted candidate across its positive, static, and negative-control checks. `fix1-freeze.txt` records exact equality: HEAD remains `05bcab93955b2070ffdc90e72eddd7368502d2c1`; the seven-file full binary diff digest remains unchanged; the implementation untracked inventory remains empty. The production `graph_recovery.py` digest remains `a0bc235eb91690b063fcef6e9533df623d136fda032b5c9b37887289330a101c`.

- Original reviewed patch, preserved without edits: `park-identity-review.diff`, SHA-256 `1a9999a08b9983d0cfb7a88cc508f1f9fab3518226d5c8e1487f55579c7a8616`.
- Test-only correction relative to that reviewed candidate: `park-identity-fix1.diff`, SHA-256 `f216c0000b189cac0bf65cd09062009c500098538bc2b282b6520b783ba1043f`.
- Complete final candidate against the unchanged base: `park-identity-fix1-full.diff`, SHA-256 `227c9c2795c1c426ee5eba865bd524ef0085c35590fe613efd50a9a71e3e8dca`.

The fix delta has one hunk in the requested refusal test only; `git apply --check --reverse park-identity-fix1.diff` succeeds against the final candidate without changing it. This worktree is left frozen for fresh scoped Astra review. Review approval, combined gates, and integration remain parent-owned and unclaimed.
