# Two intermittent failures on 0.8.12: root causes

Branch work/flakes-0812 (cut from 0.8.12 tip 5237494d5). Neither failure was
fixed here: both root causes are product behaviour with wider reach. Raw logs
were kept under `.claude/lanes/flakes/` (gitignored) and are not durable.

## A. composer-graph-fanin.spec.ts: edge set differs from topology

Failing assertion: `expect(paths.evaluateAll(data-testid...)).toEqual(topology)`
(line 129 after the quarantine toggle, line 146/150 after reload). In the
base.log failures the EXPECTED array was `[]` and the received array was the
full set, i.e. the spec's first one-shot read of the edges returned nothing
even though `paths.count()` a few statements earlier had been greater than
`count`. In argonite2.log the reverse also occurs (expected full, received 1).

### Verdict: PRODUCT DEFECT (visible flicker), exposed by a one-shot test read

Evidence, from the DOM over time (scratch Playwright spec with an init script
sampling every animation frame and every DOM mutation; spec deleted after use;
1 and 3 route cases, load and reload, 8 + 2 + 3 runs, all passing because the
probe asserted nothing):

- Every single load and reload, with no host contention, the graph goes
  blank twice after the initial fit: edge count 3 -> 0 -> 3 -> 0 -> 3 (7 -> 0
  -> 7 for the 3-route case). At requestAnimationFrame time 2 to 3 consecutive
  frames had 0 edges while 4 nodes existed, so the blank is painted, not just a
  microtask artefact (`frames-with-0-edges 2..3` of ~184 in every run).
- At each blank, all four `.react-flow__node` elements had
  `style.visibility == hidden` (React Flow 12.11.2 sets this when a node has no
  measured dimensions; `index.js` line 2346). Edges are not rendered while
  their nodes are unmeasured.
- The blanks are caused by two API responses. Timeline of one run:
  `audit-readiness` response at 706 ms -> edges 0 at 714; `validate` response
  at 715 ms -> edges back, then 0 again at 749 (the second blank follows the
  validate response's store update). With `page.route` delaying `/validate` by
  1500 ms the second blank moved from about 740 ms to 2213 ms, 6 ms after the
  delayed response (`fetch<` at 2207). The blank follows the response, not the
  clock.
- Mechanism (consistent with, but not individually proven by, the above):
  `GraphView.tsx` rebuilds `nodes` in a `useMemo` over `nodeValidationMap`
  and `nodeMessageMap` (which derive from `validationResult`), then
  `layoutGraph` returns brand-new node objects (`{...node, position}`) with no
  `measured` size. React Flow treats them as unmeasured until its
  ResizeObserver reports again, hiding nodes and unmounting edges for a few
  frames. Why the audit-readiness response also triggers a rebuild was not
  traced.

Consequence for a user: every validation result and audit-readiness update
makes the whole graph blink out for roughly 40 ms, and any click aimed at an
edge or node in that window lands on nothing. The final edge set is correct.
For the spec: it reads the edge set at a single instant right after
`sink.toBeVisible()`, which is inside that window; the spec is correct to
expect a stable topology and is detecting a real instability. CI retries hide
it. Loosening the spec (waiting for a count) would hide the defect, so it was
not touched.

### Options (not done: graph/React Flow integration)
1. Preserve React Flow's `measured` (and `width`/`height`) from the previous
   node when only validation decoration changes, or split decoration out of the
   layout memo so node identity and layout are stable across validation.
2. Keep node objects, apply validation status through a store/selector that
   React Flow can read without replacing the node.
3. Then the spec needs no change; add a probe-style assertion that edge count
   never drops to 0 between first render and settle (the sampler used here).

## B. test_colour_multi_query_contract_repair ... [happy]: calls doubled for 'red'

### Verdict: ROOT CAUSE PROVEN for the mechanism; trigger in the full run (a stall of more than 30 s) not proven. PRODUCT BEHAVIOUR: at-least-once provider calls on a wait timeout.

Path: the Composer-resolved YAML sets `max_capacity_retry_seconds: 30`
(`tests/integration/web/composer/test_colour_multi_query_contract_repair.py`
line 118). `LLMTransform` passes it as `batch_wait_timeout`
(`plugins/transforms/llm/transform.py:1817`). When the engine's
`waiter.wait(...)` (`engine/executors/transform.py:624`) exceeds it,
`TimeoutError` is raised, the node state is failed, `evict_submission` removes
the buffer entry (`transform.py:973-983`) and the retry resubmits the whole
row. The first worker is NOT cancelled: it finishes its provider call and
`_complete_ticket` logs `late_result_discarded reason=timeout_evicted_or_shutdown`
(`plugins/infrastructure/batching/mixin.py:360`). The retry re-issues every
query of the row, so both of red's calls doubled while nine other entries
matched.

Evidence:
- The failing full-run log (pytest.log near line 148113) contains exactly that
  `late_result_discarded` debug line in the test's captured stdout, 52 s after
  the test's first log line (the test takes 5.1 s serially, 3 of 3 serial runs
  passed in 13 s).
- Deterministic reproduction (scratch test, not committed; saved untracked at
  `.claude/lanes/flakes/repro_dup_provider_call.py.txt`): same pipeline with
  `max_capacity_retry_seconds: 2` and the first `red/good_colour_pair`
  provider call held for 5 s. Delay 0: pass. Delay 5 s: fail with the same
  differing items `('red','good_colour_pair'): 2 != 1` and
  `('red','approximate_hex'): 2 != 1`, the `late_result_discarded` line, and
  provider-call stamps 5 s apart for the same prompt.
- Ruled out: shared state between parametrisations/workers (the SDK double,
  its `calls` list and its patches are per-test; xdist workers are separate
  processes; the run's other two scenarios passed); resume/reclaim (no run
  resume involved); a transient error inside the double (it cannot raise).

### Product finding: duplicate provider calls
Under ordinary load, if the engine does not get a row's result within
`batch_wait_timeout` the row is retried while the original provider call is
still in flight. With a real provider that is two billed calls for one row,
and the Composer-generated pipelines carry a 30 s bound (default for hand-written
settings is 3600 s). The audit trail's call count for such a run exceeds the
row count by design ("Original worker may still complete, but result is
discarded", transform.py comment).

Not proven: what stalled the first row for over 30 s in the full run (host
load, a first-call import, or thread starvation). The double returns
instantly, so the wait expired on scheduling, not on the provider.

### Options (not done: engine execution semantics)
1. Make the evicted worker cooperative: pass a per-attempt cancel event so a
   retry does not start until the first attempt is cancelled or finished.
2. Do not retry a batch-wait timeout while the original worker is alive; fail
   the row instead (honest, no duplicate cost).
3. Test side: not by stretching the 30 s (disallowed); instead assert on
   unique `(colour, query)` plus separately assert provider-call accounting,
   or make the double deterministic about a stall. Only valid once the
   product decision above is made.
