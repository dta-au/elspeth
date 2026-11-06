# Composer memo working-set follow-up, 7 November 2026

Independent Fable 5.1 follow-up to the [holistic review](composer-holistic-timing-caching-validation-2026-11-07.md), pinned to published `66903e553ac8eec06336d828f4c73b1a212bfb66`.

Root's subsequent controlled experiment did **not** recover the deadline. On the
same frozen code and unchanged five-second preparation clock, 32 MiB took
5.643582874 seconds and 64 MiB took 5.532289734 seconds at first END. Both actual
constructed parent budgets were observed; both native runs exited 1 with complete
cleanup, and the temporary setting was restored. The observed 0.111293140-second
difference is insufficient to justify adopting the allocation as the deadline
repair. Production stays at 32 MiB; document/evidence and operation-payload limits
were unchanged. Receipts are R367 and R369 in the local takeover lane.

The review below preceded those results. Its recommendations are conditional
analysis, not product acceptance or instructions to run every suggested experiment.
The CLI returned a final answer with child exit 0; its supervisor returned 125
for abandoned descendants and completed cleanup. This review supplies no native
runtime or memory qualification.

---

**Recommendation: yes. Test the bounded 64 MiB per-action memo first, and adopt it if the conditions below hold. Defer key and family redesign; do not start it now.** I expect 64 MiB to recover roughly 0.2–0.6 s, so it may not reach the 5 s endpoint alone, and it moves a history-size cliff rather than removing it.

I accept the corrections: 3,570 SQL / 45 workers, and R338's finding that the final reads are not dominated by writers. That was my low-confidence inference, and it is withdrawn.

Labels: **[F]** source fact at `66903e553a` (the memo and limits files are byte-identical to what I read at `c900eebd9c`; both diffs are empty). **[O]** observed in the R333 artifact. **[I]** my inference. **[P]** experiment pending with Root.

## Why the budget comes first

- **The pressure is measured.** Peak 33,513,114 of 33,554,432 bytes; recorder 18 admissions, 14 evictions, 11 recomputations; distinct-key estimate 39,144,279 bytes, about 5.6 MB over budget. [O]
- **Decoded recorder data is the heavy evicted family, and digest keys cannot shrink it.** Recorder accounts for 25.7 MB of about 29.4 MB evicted. [O] R360 is right on this point, and it corrects the "entry design, not budget size" reading in my earlier table.
- **The recorder is thrashing in a small residual, not the whole budget.**
  - Recorder insertion evicts its own family first (`continuation_retention_memo.py:1205-1207`). [F]
  - Evicted recorder entries average about 1.84 MB (25,709,812 / 14). [O]
  - Seven distinct entries are therefore about 13 MB, a third of the 39 MB estimate. The text-proof families hold the other two thirds, leaving roughly 7 MiB, or three to four entries, for seven distinct recorders. [I]
- **So 64 MiB should fit this working set with about 27 MiB to spare.** Something near 48 MiB probably would too. [I]
- **Digest keys could also relieve it, by freeing the proof-family share.** That costs a new trust argument and new code, where the budget is one reversible constant. [I]

## Prediction for Root's pending arms

- **Size of gain.** [I] In R333 the recorder factory was 56 of 519 samples, and 11 of 18 constructions were recomputations. Removing them, plus nine small recomputations in other families, is worth roughly 7–9% of preparation.
- **Whether it crosses 5 s.** [P] That depends on the current 32 MiB control after the O2/O5 and semantic-comparison changes, which I have not seen. It could cross if the control is already near 5.4 s; otherwise probably not.
- **If the gain is much larger than about 0.6 s,** suspect a difference between arms other than the budget.

## Adoption conditions if the original endpoint improves

1. **The arm tested what it claims.** The constructed parent budget is observed as 64 MiB, on the same tree, run serially, with the clock and limits unchanged.
2. **The gain exceeds noise.** Near-identical trees already differed by 0.04 s (5.806 vs 5.846). [O] Use at least three alternating runs per arm, and require a pass with margin, not at 4.98 s.
3. **The mechanism is confirmed once.** One observer-enabled 64 MiB run, not used for timing, should show recorder recomputations near zero, peak well under 64 MiB, zero budget violations and an empty close. If the endpoint improves but evictions did not fall, the budget did not cause it.
4. **Recorder per-entry eligibility stays at 32 MiB.** Today it equals the parent budget (`:1183-1185`). [F] All 18 recorder misses were admitted at 32 MiB, [O] so this costs the fixture nothing and confines the side effect you flagged. It is one comparison, not a framework.
5. **One four-job concurrent run** records the RSS difference between 32 and 64 MiB, compared against real deployment headroom.
6. **One history probe at 64 MiB:** the same route with one more provider call, recording peak and evictions. This locates the next cliff.
7. **Update the pinned test value and the comment** at `continuation_limits.py:16-20`. The comment still describes "Advisor text proofs", although the budget now holds decoded recorder data. [F]

Later stages reached after a pass should be reported separately.

## Memory and hostile-outsider risks

- **Assume every active parent sits at its cap.** A benign four-call fixture already saturates 32 MiB, [O] so a hostile user or provider can fill 64 MiB trivially with valid content. The realistic charge is 256 MiB at the default four jobs and 1 GiB if concurrency is set to its maximum of 16 (`web/config.py:422`). [F] An operator raising concurrency without knowing this is the accidental-error case.
- **Charged bytes are not RSS.** The counter excludes:
  - transient decode and encode allocations;
  - evicted values still referenced by callers;
  - the SQL-child tables, which have no byte budget (`:497-507`, `:1233-1251`); [F]
  - allocator high-water marks.

  In the thrash regime a larger budget may even lower real peak memory, because fewer recomputed duplicates are alive at once. [I, unmeasured]
- **Decoded recorder size is not bounded by the 1 MiB document limit.** Expansion scales with calls times per-request size (R330's mechanism). [I] The decode happens whether or not the result is retained, so the raise adds residence, not allocation capability.
- **Without condition 4, one inflated recorder dominates.** It could occupy the whole budget and, by eviction order, flush every other family. The sizer walk for an oversize entry also does up to twice the traversal before declining. [F for the mechanism]
- **Residence is short.** The parent closes at the preparation cut (`:1327-1331`; call site `continuation_checkpoint_end_physical.py:379`), [F] so a slow provider cannot pin it. A slow preparation can hold it for its full run.

## When to stop increasing the budget

- **Evictions are already near zero or peak sits well under the cap.** More cannot help; 96 MiB has no supporting evidence.
- **Evictions vanish but the endpoint gain is within noise or still over 5 s.** The remaining work is not cache-bound.
- **The history probe shows the working set outgrowing 64 MiB within one or two more provider calls.** The recorder share grows roughly with calls squared. [I] That is the trigger for representation or key work; another doubling would not hold.
- **Budget times configured concurrency approaches deployment headroom,** or measured RSS grows materially faster than the charged difference.
- **A single entry approaches half the budget.** The cap is then being sized by one inflatable object.

## Confidence Assessment

**Overall Confidence:** Moderate.

| Finding | Confidence | Basis |
|---|---|---|
| Budget saturated; recorder thrash is budget-driven | High | R333 counters; `:1205-1207` |
| Recorder is about a third of distinct weight | Low–Moderate | Averages from evicted bytes only |
| Gain of about 0.2–0.6 s | Low–Moderate | Instrumented sample proportions on an older tree |
| Eligibility and charged-stock side effects | High | `:1183-1185`, `config.py:422` |
| 64 MiB holds only for modestly longer histories | Low | Growth mechanism, not measured |

## Risk Assessment

**Implementation Risk:** Low. **Reversibility:** Easy (one constant).

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| Adopted on a noise-level gain | Medium | Medium | Conditions 2 and 3 |
| Memory pressure at high concurrency | Medium | Low–Medium | Conditions 4 and 5; state budget × concurrency next to the setting |
| Cliff reappears on longer histories | Medium | High | Condition 6 and the stop rules |

## Information Gaps

1. [ ] **The 32 and 64 MiB results on the current tree.** [P]
2. [ ] **Per-family distinct weight.** It is not in the artifact and would confirm the two-thirds split.
3. [ ] **Production memory limits and replica count.**
4. [ ] **The bound on calls per operation and request size,** which caps decoded recorder expansion.

## Caveats & Required Follow-ups

- **Assumptions:** one action parent per active job; the R333 proportions transfer to the current tree.
- **Limitations:** I ran nothing; I did not re-read the test helper or R338.
- **Next steps:** wait for the two arms, then apply conditions 1–3 before deciding. Conditions 4–7 belong to the production change, not the experiment.
