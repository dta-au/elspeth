---
title: Transform on_error parity test varies two things at once, so it cannot discriminate
labels: [area/tests, area/composer, type/bug, priority/P3]
---

A test that is supposed to prove a graph comparator notices a change to a transform's error
policy changes two things between the two graphs, so it would pass even if the comparator
ignored the error policy entirely.

## Where to start

`tests/integration/web/composer/parity/test_composer_graphs.py:261-270`. The sibling test two
functions below, at `:279-300`, does the same job correctly and is the model to copy.

## Why

The test deep-copies a graph, changes the transform's `on_error` to `discard`, and also removes
the `errors` output from the copy, then asserts that the comparison raises:

```python
right["nodes"][0]["on_error"] = "discard"
right["outputs"] = [o for o in right["outputs"] if o["name"] != "errors"]
```

The comparator rejects a difference in the output inventory on its own, so the raise cannot be
attributed to `on_error`. The gate and coalesce version below keeps `errors` on both sides and
says so in a comment, which is why that one discriminates.

## Impact

Contained to the test suite; no production behaviour is alleged to be wrong, and the residual
risk is nil. The comparator is a test helper (`tests/helpers/composer_graphs.py`, with
`IsomorphismError` at `:66` and `assert_isomorphic` at `:490`), and at `:242-253` it emits
`on_error` unconditionally for every non-queue node — there is no per-node-type branch a
transform could fall out of. The gate and coalesce test already exercises that exact line
without the confound, so transform error-policy preservation is in fact covered, just not by
the test named for it.

## Fix

Keep the `errors` output present on both graphs and vary only the `on_error` target, mirroring
the structure of the test at `:279-300`.

You would know it holds when the comparison still raises with the output present on both sides,
and when making the two `on_error` values equal again stops it raising. The second half is what
proves the assertion is reading `on_error` rather than the output list.

**Size.** About three lines, self-contained, no product decision, and one file to read to
understand it — a reasonable first contribution to this repository.
