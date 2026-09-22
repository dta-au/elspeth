---
title: pdf_rasterize does not enforce max_page_bytes or the PNG contract on worker output
labels: [area/plugins, type/bug]
---

The PDF rasterise transform reads whatever bytes its render worker leaves in the output
directory, stores them, and labels them `image/png` — without checking them against the
configured `max_page_bytes` or against the PNG format. The configured cap is a claim made by
the less-trusted worker rather than a boundary the parent enforces.

## What happens

Reproduction: a fake raster worker returns a contained path holding roughly 1 MiB of
`NOT_A_PNG` bytes, while the transform is configured with `max_page_bytes: 64`. The transform
succeeds, persists the full payload, and labels it `image/png`.

## Why

In `_map_rasterize_response()` in `src/elspeth/plugins/transforms/pdf_rasterize.py`, each
worker-returned page path is resolved and checked for containment inside the temporary render
output directory; a path outside it raises a containment-breach error. Once that check passes,
the parent calls `read_bytes()` on the file, stores the bytes in the payload store, records
`len(data)` as the page size, and sets the page MIME type to the module constant
`PAGE_MIME_TYPE` (`"image/png"`). There is no comparison against `max_page_bytes` and no
inspection of the file's signature or content. `max_page_bytes` is passed into the rasterise
request as the worker's own limit and is never re-checked on return.

Reviewed at commit `3dc67fb1d`. The mechanism is unchanged at the current tip, although the
line numbers recorded in the original report have since moved.

## Impact

A defective or compromised worker can exceed the byte cap, cause an unbounded parent-side
read, and publish arbitrary bytes downstream under a false MIME type. The containment check
does limit *which* file can be read, so this is not an arbitrary-file-read: the escape is in
size and content, not in location.

## Fix

The parent enforces the actual output size and the PNG content contract before storage:
check the on-disk size against `max_page_bytes` before reading the file (or read bounded),
verify the PNG signature, and treat a violation as one of the typed page refusals the
transform already has for oversized pages, rather than persisting the payload. A test with a
worker that returns oversized or non-PNG bytes must fail the document or refuse the page
instead of succeeding.
