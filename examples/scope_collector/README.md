# Scope + Collector — Closing An EXPAND Group With A Barrier

Demonstrates `scopes:` and `collectors:` — the barrier that closes a multi-row
expansion, and the `policy` knob that decides what an incomplete group means.

Two configs ship here. Diff them: `policy` and the two output paths are the
only differences.

| Config | Policy | DOC-2 (one page lost) | Output rows |
|--------|--------|-----------------------|-------------|
| `settings.yaml` | `require_all` | no statistics row at all | 2 |
| `settings_best_effort.yaml` | `best_effort` | `mean: 20` over 2 of its 3 pages | 3 |

## What This Shows

```
source (3 docs) ─(rows)─> [explode_pages] ─(page_in)─> read_value ─(pages)─> [page_stitcher] ─> out
                           ^ scope opener                                     ^ scope closer
```

`documents.jsonl` holds three documents of 3, 3 and 2 pages. DOC-2's page 2
carries the reading `"smudged"` instead of a number, so `read_value` fails for
that member and it is discarded — which is exactly the lost member the policy
has to rule on.

## Running

```bash
elspeth run --settings examples/scope_collector/settings.yaml --execute
elspeth run --settings examples/scope_collector/settings_best_effort.yaml --execute
```

**Both end PARTIAL with exit 1, by design.** A member is lost by construction in
both. The exit code does not distinguish the policies — the output rows do:

```console
$ cat output/require_all.jsonl
{"batch_size": 3, "count": 3, "mean": 15, "sum": 45}     # DOC-1, whole
{"batch_size": 2, "count": 2, "mean": 10, "sum": 20}     # DOC-3, whole
                                                          # DOC-2: nothing

$ cat output/best_effort.jsonl
{"batch_size": 3, "count": 3, "mean": 15, "sum": 45}     # DOC-1, whole
{"batch_size": 2, "count": 2, "mean": 20, "sum": 40}     # DOC-2, 2 of 3 pages
{"batch_size": 2, "count": 2, "mean": 10, "sum": 20}     # DOC-3, whole
```

That `mean: 20` is the reason the policy exists. It is a real number over a real
denominator of two, and nothing about the row says the document has three pages.
Under `require_all` you get no number instead — because a mean over an
incomplete document is not a cheaper mean, it is a different measurement.

## The Loss Is Recorded Either Way

Absence is explicit, never an implied clean result. Both runs write a
`group_losses` row:

```bash
sqlite3 examples/scope_collector/runs/require_all.db \
  "SELECT closer_name, group_id, token_id, reason FROM group_losses"
```

naming the closer, the group, the lost token and the reason. `group_records`
carries the group's `member_count` as the opener actually produced it, so the
gap between declared membership and arrived members is reconstructable after
the fact under either policy.

## A Collector Is Not An Aggregation

It reuses the batch-transform plugin contract — same plugins, same options — but
it is a barrier, not a window, and the differences are load-bearing:

**No trigger config, deliberately.** `count`, `timeout_seconds` and `condition`
are inexpressible on a closer. A count trigger cannot know where a group ends,
so a lost member does not shorten the batch — it **backfills from the next
group**. Configure `count: 100` over a document that expands to 100 pages, lose
one, and the flushed batch holds 99 pages of that document plus one page of the
next, with honest-looking counts, no short read and no audit anomaly. That
contamination is undetectable by construction. A timeout on a closer is worse
again: it converts a liveness bug into a silently short group.

**Flush order is the opener's expansion ordinal**, never arrival order. Page 2
reaches the stitcher after page 1 even if it finished first.

**Membership comes from the engine.** The scope binds opener to closer at build
time, so the group's cardinality is whatever the opener actually produced for
that row — not a number you configured and hoped matched.

## Configuration Notes

- The opener **must** be a multi-row transform (`creates_tokens = True`). The
  builder enforces this, because config time cannot see plugin attributes.
  `json_explode`, `line_explode`, `blob_csv_expand` and `pdf_rasterize` qualify.
- The closer **must** name a `collectors:` entry, not a transform.
- `policy` is **required and has no default**. The author decides whether a lost
  member fails the group; the engine will not decide it for you. `quorum` and
  `first` are not implemented.
- Ordinary transforms may sit between opener and closer — they run inside the
  bound region, as `read_value` does here.
- `on_error` on a collector is optional. Omitted, the route derives from
  structure and losses settle through the scope's group machinery rather than
  through a configured error edge.

## See Also

- `examples/json_explode` — the same opener with no scope, so the expanded rows
  simply flow onward and nothing closes them
- `examples/pdf_rasterize` — a real expand group (PDF pages) with a malformed
  document quarantined
- `examples/batch_aggregation` — a windowed `aggregations:` batch, which is the
  thing this example is deliberately not
