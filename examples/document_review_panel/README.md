# Document Review Panel — The Stack Unrolls

The most complex example in the corpus. It combines a fork (two LLM reviewers
per page) with an EXPAND group (pages of a document closed by a collector), and
its subject is what one lost token costs at each level above it.

**The row is the unit of pass/fail.** Everything below follows from that.

## The Three Levels

```
docs.jsonl ─(rows)─> [explode_pages]  ◄── SCOPE OPENER, one token per page
                          │
                          ├─> [panel_fork] ─┬─ arm_a ─> review_a  (reviewer-strict)
                          │                  └─ arm_b ─> need_evidence ─> review_b  (reviewer-lenient)
                          │                        ↓
                          │              [panel_merge]  coalesce, policy: require_all
                          │                        ↓  ONE page row: review_a_score + review_b_score
                          ├─> [disagreement]  abs(a − b)
                          ↓
                    [doc_verdict]  ◄── SCOPE CLOSER, policy: require_all
                          ↓  one verdict row per document
                    [corpus_summary]  aggregation, trigger {}
                          ↓
                         out
```

| Level | Mechanism | Control edge? | On a lost member |
|-------|-----------|---------------|------------------|
| **Page** | `coalesce` `policy: require_all` | yes — declared branches | the page fails |
| **Document** | `scopes` `policy: require_all` + collector | yes — closer binding | the document verdict is refused |
| **Run** | `aggregations` window, `trigger: {}` | **no** | computes over survivors |

Levels 1 and 2 have a control edge, so they can fail closed. Level 3 has no
closer binding, so it takes what it can get. That is the designed behaviour, not
a defect — **without a control edge you take what you can get**, and the run
status is the signal that you did.

## Running

```bash
./examples/document_review_panel/run.sh
```

Three configs, one local ChaosLLM server, each with its expected exit code
asserted:

| Config | Corpus | Ends | Published |
|--------|--------|------|-----------|
| `settings.yaml` | 4 docs, 12 pages, all complete | COMPLETED, exit 0 | summary over **4** documents |
| `settings_incomplete.yaml` | identical, but ONE page lacks ONE field | PARTIAL, **exit 1 by design** | summary over **3** documents |
| `settings_run_as_row.yaml` | the same loss, run encapsulated as one row | PARTIAL, **exit 1 by design** | **nothing at all** |

The two corpora differ only in that `documents_incomplete.jsonl` omits the
`evidence` key from SUB-02 page 2. One key.

## The Cascade, From One Token

In `settings_incomplete.yaml`, the lenient reviewer weighs cited evidence and
will not review a page that cites none. That single page's arm B is discarded,
and the loss climbs:

```
group_losses: panel_merge / quarantined     ← L1: a reviewer was lost, so the PAGE failed
group_losses: doc_verdict  / group_failed   ← L2: a page was missing, so SUB-02's verdict was refused
corpus_summary: {"count": 3, ...}           ← L3: the number is over 3 of 4 documents
✗5 failed, PARTIAL, exit 1
23 llm calls                                ← 24 minus the one arm never spent
```

Read the ledger, not just the exit code:

```bash
sqlite3 examples/document_review_panel/runs/incomplete.db \
  "SELECT closer_name, reason, COUNT(*) FROM group_losses GROUP BY 1, 2"
```

Every level records its own loss under its own closer. The unroll is
reconstructable after the fact; nothing is absorbed silently.

**And note what level 3 does not say.** `{"count": 3}` is a true statement about
three documents. Nothing in that row records that a fourth was attempted and
refused. The run status is the only in-band signal, which is why exit code and
row content must both be read — a consumer that stores the row and drops the
status has stored a short number as if it were whole.

## Making The Whole Run Fail Closed

`settings_run_as_row.yaml` is the answer to "I want level 3 to fail closed too."

You cannot bolt a control edge onto a window. What you can do is **move the row
boundary**: encapsulate the run inside a single row and batch it internally.
There, the whole corpus is one source row, the documents are members of that
row's EXPAND group, and `require_all` on the scope therefore spans the entire
corpus.

Same loss. Same policy keyword. Completely different blast radius:

```
settings_incomplete.yaml   4 docs, 1 fails  ->  a number over 3 is published
settings_run_as_row.yaml   4 docs, 1 fails  ->  NOTHING is published (✓0 succeeded)
```

The empty sink is the pass condition for that config, not a broken run — the
launcher asserts the file is empty or absent.

**Choosing where the row boundary sits is choosing what completeness means.**
The price is real: the corpus is now one row, so per-document work is no longer
independently retryable, and one bad document costs every other document's
published result. Encapsulate when a partial answer is worse than no answer — a
regulatory return, a reconciliation, a published statistic — and not otherwise.

## Design Notes Worth Copying

**The metric is disagreement, not mean score.** The document verdict is computed
over `abs(review_a_score - review_b_score)`, so both arms are load-bearing by
construction. A mean over whichever reviewer happened to answer would still
produce a plausible number with one arm missing; a disagreement cannot be
computed at all. The metric is chosen so a silently-lost arm is arithmetically
impossible to paper over — and the clean run's `✗0 failed` is therefore positive
evidence that both reviewers survived the merge.

**The two arms use distinct query names.** `review_a` and `review_b`, so the
merged page row carries `review_a_score` and `review_b_score`. Sharing a query
name would collide under `merge: union` and one reviewer would vanish silently
at exit 0 — a real trap, hit while building this example.

**`coalesce`, not `row_union`.** A collector requires one token per member, and
`row_union` preserves cardinality (it would release two tokens per page).
Placing a `row_union` here currently reaches a Tier-1 audit-integrity error
rather than a build-time rejection — `elspeth-9db785ace7`.

**`on_error: discard`, never a sink, inside a merge branch.** A sink route there
builds a DIVERT edge that the builder warns about by name; the group fails
closed regardless, so the sink promises a recovery path the barrier will not
honour. Relatedly, a sink cannot sit inside a bound region at all.

**The document verdict is statistical, not an LLM summary.** A collector must be
a batch-transform plugin, and ADR-020 retired both batch-LLM transforms for
breaking per-row attribution. The per-PAGE judgements are LLM work; the document
and corpus levels are statistics over them. No model writes the verdict.

**Fault injection is zeroed.** The one loss is authored into the data. An
injected fault would make *which* document fails a lottery and every documented
count meaningless.

## See Also

- `examples/scope_collector` — the collector barrier and its policy in isolation
- `examples/ab_llm_experiment` — the fork/`row_union` A/B, including what a lost
  arm costs a single row
- `examples/fork_coalesce` — coalesce merge policies without a surrounding scope
