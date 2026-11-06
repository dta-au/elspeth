# Confirmed gaps and implementation tasks

Refreshed 2026-11-06 against `ae88147c6130051b1e3d497a1341a69bd56924e5`.
G01/G02 are materially narrower than the original audit. G03–G07 were rechecked;
their cited contradictory test/document anchors are unchanged. Source line
numbers are navigation hints; no test failure here is represented as newly run.
O03's separately inspected existing execution evidence is in OPEN-QUESTIONS.

| ID | Unit | Category | Priority/dependency |
|---|---|---|---|
| G01 | U37, U38 boundary | Remaining automatic rollover cuts | Later-Compose cooperative rollover received; resolve remaining cuts |
| G02 | U46, Q | Two incompatible source guards | Authority and session service; final re-derivation after source edits |
| G03 | U30b | Stale test API usage | Bounded test migration |
| G04 | U34 | Stale PostgreSQL assertions | Align with actual schema contract |
| G05 | U09 | Stale legacy test/reference | Preserve current refusal boundary |
| G06 | U53 | Operator-facing release inconsistency | Align after final schema/policy changes |
| G07 | U12 | Model guidance/discovery omission | Bounded documentation and test correction |

## G01 — automatic successor rollover is narrower than the required behavior

**Current disposition: partial; completed portion removed from the task.**
`plan_automatic_successor` now accepts `action_fit`, `watermark` and `quantum`.
The driver reads actual SQL timing, accounts for elapsed read time, selects the
next action and invokes the real atomic successor/readback path. The previous
claim that adequate hard headroom always prevents cooperative rollover is closed.

The remaining restrictions are explicit: exactly one handoff-safe action-ready
Compose frame with committed PRIMARY lineage, no pending result and no open
effect. First Compose, non-Compose and applied-batch cuts retain same-sequence
recovery. Those remaining cuts are the active scope question.

**Requirement.** The detailed design requires finishing an admitted healthy
action and yielding at the quantum/watermark boundary. The audit found no
explicit supersession of that successor requirement. Existing same-sequence
recovery is real and tested; this finding concerns rollover breadth.

**Evidence.**

- [Successor eligibility](../../../src/elspeth/web/composer/continuation_action_fit_yield.py), lines 27–88; first/non-Compose restriction at 43–45 and 68–79.
- [Admission clocks](../../../src/elspeth/web/composer/continuation_clock.py), lines 50–75.
- [Driver](../../../src/elspeth/web/sessions/continuation_driver.py), `yield_control` at 1562–1636, actual timing and run decisions at 1724–1761.
- [Detailed requirement](../../design/composer-continuation-plan.md), lines 449–477.
- [Runtime design](../../architecture/design-notes/0.8.10-continuation-runtime-design.md), quantum-successor scenario near line 1319.
- [Actual successor tests](../../../tests/unit/web/sessions/test_continuation_action_fit_successor.py), lines 319–434, parameterized quantum/watermark: original SQL clock and real elapsed waiting, sequence 2→3, preserved ownership/counters and no PRIMARY replay. This scenario deliberately Stops after the next PRIMARY; it asserts cancellation, not whole-request completion.
- The same file at 437–517 holds a healthy admitted PRIMARY across the cooperative boundary and completes through same-sequence recovery. It establishes healthy-action completion, not a successor at that cut.
- [Actual model-control successor tests](../../../tests/unit/web/sessions/test_continuation_control_successor.py), lines 94–249.

**Task.** Map the remaining required first-Compose/non-Compose/applied-batch cuts
to their current recovery behavior. Prove their cold ownership before extending
automatic successor eligibility, or record an explicit scope disposition. Keep
the completed later-Compose cooperative path and its tests. Do not remove safety
guards blindly, yield through an open/uncertain paid effect, fabricate
a new provider response, bypass the provider, or reset request-wide counters.
If a current explicit ruling narrows the requirement, cite it and adjust the
disposition instead of implementing a conflicting design.

**Acceptance.** Preserve existing real elapsed-clock quantum/watermark cases.
For any newly admitted cut, prove the required successor behavior and its actual
timing/ownership path. Verify predecessor/successor
lineage, one human request, original paid-effect ownership, no paid replay,
cumulative counters, slice reset, and Stop/CAS opposition. Preserve model-control,
hard-budget action-fit and legitimate recovery cases. PostgreSQL ownership and
process-drain checks remain applicable. Reject a change that merely renames
same-sequence suspension as successor yield.

## G02 — two full-source proof bindings remain stale

**Observed.** All 13 entries were compared against exact Git blobs with matching
source and one-byte mutation controls. The count fell from 7 at a6990ffb to 1 at
c9f568e63b, then became 2 when ae88147c changed session service. Current paths:

```text
src/elspeth/web/coordination/composer_operation_authority.py
src/elspeth/web/sessions/service.py
```

**Evidence.** [Reviewed source guards](../../../tests/helpers/required_work_reserve_recipes/reviewed-full-source-guards.json);
[fencing proof](../../../tests/unit/architecture/test_web_landscape_mutation_fencing.py),
lines 6178–6197 emit/propagate `full source changed`, 22382–22395 stop reserve
proof construction on load failures, and 36678 rejects global failures.
The committed input combination cannot satisfy that assertion as written.
The other 11 matching guards are no longer active stale-binding findings;
hash agreement alone does not qualify the complete proof.

**Task.** Review each guarded source change and its reserve/fencing implications.
Re-derive the relevant proof inputs using the repository's supported mechanism
against the final integrated source. Record the substantive review. Do not
blindly replace hashes, relax the binding check, or confuse these source hashes
with suspended operator signatures.

**Acceptance.** Final exact-source comparison agrees for every declared guard;
the actual proof gate passes on a frozen tree; an appropriate planted source or
guard mutation is rejected. Validate the ordinary/proof shard partition as
applicable. The prior extra-marker/catalog mismatch is already fixed: do not
reopen it without new evidence. Repeat re-derivation if a guarded source changes.

## G03 — test migration retains removed Composer service APIs

**Observed.** [Service tests](../../../tests/unit/web/composer/test_service.py),
lines 2508/2526/2532, use retired `_advance_live_compose_iteration` and the old
compose path on a real `for_trained_operator` service. [Advisor binding tests](../../../tests/unit/scripts/test_composer_advisor_binding.py),
lines 855–856, dereference `_dispatch_tool_batch` and
`_advance_live_compose_iteration`. The class and inspected fixture adapters do
not supply those methods. Production dispatch is now
[service.advance_continuation_phase](../../../src/elspeth/web/composer/service.py),
lines 2356–2380, with the live phase owner.

**Task.** Migrate the assertions to the actual current owner/entrypoint. Preserve
the flagged-to-clean repair-cycle test's semantic purpose: advisor exchange
elision before finalization. Reconcile the advisor binding census with actual
owners. Do not add dead compatibility methods to production to satisfy tests.

**Acceptance.** Run the affected tests through real current ownership. Include
opposition that would catch loss of exchange elision and incorrect owner/binding
discovery. Keep any broader dynamic-attribute or census contracts satisfied.
Inspect existing [live phase tests](../../../tests/unit/web/sessions/test_continuation_live_phases.py)
before duplicating their transition/Stop/CAS assertions.

## G04 — PostgreSQL schema tests assert epoch 85 instead of 88

**Observed.** [PostgreSQL schema probes](../../../tests/testcontainer/web/test_schema_probe_postgres.py),
lines 2027, 2050 and 2068, require `SESSION_SCHEMA_EPOCH == 85` while
[models](../../../src/elspeth/web/sessions/models.py), line 97, and
[schema](../../../src/elspeth/web/sessions/schema.py), line 61, require 88.
The affected tests cover current-epoch CHECK drift, reflection/idempotence and
paid-incomplete-effect checks.

**Task.** Re-read each complete test and preserve its deliberate version-bound
contract when aligning it with the current schema. Do not replace a useful
literal-version assertion with a tautology or weaken drift detection.

**Acceptance.** The three cases run serially on PostgreSQL and still reject their
intended schema mutations. Preserve/refuse and current-schema idempotence remain
covered. Follow repository policy for the full marked PostgreSQL gate.

## G05 — obsolete legacy-publication test and reference promise

**Observed.** [Legacy source test](../../../tests/unit/web/sessions/test_content_publication_legacy_source.py),
lines 54–68, writes removed scalar `source`, sets `sources=None`, omits content
publication, and expects legacy decode with no publication. It is not skipped.
The current table uses `sources`; [publication decoding](../../../src/elspeth/web/sessions/content_publication.py),
lines 61–63, rejects missing publication. [The reference](../../reference/composer-content-publication.md),
lines 24–41, still promises the old compatibility behavior.

**Task.** Replace or retire obsolete acceptance assertions in favor of the
current preserve/refuse contract; retain meaningful negative coverage of missing
or malformed publication and valid current rows. Correct the reference. Do not
restore removed schema compatibility or migrate stores as part of this fix.

**Acceptance.** Current valid publications decode; missing/malformed publication
is refused at the intended boundary; publication/self-revert and ownership tests
retain their behavior. Run affected tests and relevant PostgreSQL checks if any
persistence implementation changes.

## G06 — release surfaces advertise incompatible schema/policy facts

**Observed.** Actual Sessions epoch is 88; Landscape is 50. Maintained documents
advertise Sessions 84, 85 or 82:

| File | Audited location/value |
|---|---|
| [README](../../../README.md) | 109, 146–164: 84 |
| [CHANGELOG](../../../CHANGELOG.md) | 74, 238–252: 84 |
| [Website](../../../website/get-started.html) | 265: 84 |
| [Upgrade draft](../0.8.10-upgrade-notes-draft.md) | 137–176, 537–563: 85 |
| [Fresh-store cutover](../../runbooks/staging-session-db-recreation.md) | 12–38, 84: 82 |
| [ECS runbook](../../runbooks/aws-ecs-deployment.md) | 1643: acceptance JSON 82 |
| [ACA runbook](../../runbooks/azure-container-apps-deployment.md) | 576: acceptance JSON 82 |

[Acceptance schema facts](../../../src/elspeth/web/_acceptance_common/schema_facts.py),
lines 56–81, derive current constants; documentation tests compare those facts.
CHANGELOG lines 263–266 also confines unsupported repair-budget refusal to empty
graphs, although the current admission policy applies to existing graphs too.
The master plan's amended A8 epoch80 wording is also historical and stale.
Additionally, CHANGELOG at 246/249–252 says the live policy admits V2 and does not
activate V3/V4; current `continuation_policy_settings.py:57–100` constructs
`ContinuationAdmissionPolicyV4`. These are policy payload formats, not public
lifecycle versions. Correct the live-policy claim without retaining obsolete
formats solely for compatibility under the 6 November ruling.

**Task.** Align current release/operator instructions with the final actual
schema and repair-policy boundary. Distinguish historical examples from current
acceptance expectations; do not globally rewrite archival epoch references.
Preserve fresh-store/preserve-refuse and reload-open-tabs instructions. Update
version-bound oracles with the same deliberate review as other gate edits.

**Acceptance.** Release/README/site/cutover/acceptance-document tests agree with
the final code; malformed release/date/schema controls still fail. Operators can
follow ECS and ACA instructions without encountering mutually inconsistent
current epoch values. Documentation must not imply a migration is supported.

## G07 — continuation-control result teaching is omitted

**Observed.** [Provider palette](../../../src/elspeth/web/composer/provider_gateway.py),
lines 483–500, offers `checkpoint_and_continue`. Accepted output is plain text;
refusal is a status object. [Model-facing guidance](../../../src/elspeth/web/composer/skills/pipeline_composer.md),
lines 378–396, and [tool reference](../../reference/composer-tools.md), lines
641–651, omit that result-form exception. [The teaching test](../../../tests/unit/web/composer/test_tool_result_variant_teaching.py),
lines 37–59, inventories inline registry tools plus recovery names, so cannot
discover this offered control.

**Task.** Teach the actual accepted/refused forms and make exceptional-result
discovery cover the effective offered palette, including continuation controls.
Use the real declaration/response contract, not another hand-maintained count.
Do not change the already-connected control semantics just to simplify prose.

**Acceptance.** Actual continuation result shapes agree with both guidance
surfaces. Removing its teaching or omitting it from discovery is detected by a
meaningful test/control. Existing registry/recovery result teaching still works.
