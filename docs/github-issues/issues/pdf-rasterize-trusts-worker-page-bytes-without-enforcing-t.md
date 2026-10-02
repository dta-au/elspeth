---
title: pdf_rasterize does not enforce max_page_bytes or the PNG contract on worker output
labels: [area/plugins, type/bug]
---

The PDF rasterise transform reads whatever bytes its render worker leaves in the output
directory, stores them, and labels them `image/png` — without checking them against the
configured `max_page_bytes` or against the PNG format. The configured cap is a claim made by
the worker rather than a boundary the parent enforces.

## Background

ELSPETH's transform plugins live in `src/elspeth/plugins/transforms/`. `pdf_rasterize` renders
each page of a PDF to a PNG in a separate worker process, because parsing hostile PDF bytes
in-process is not acceptable. The parent then reads what the worker produced and publishes it
into the run's payload store. Everything the worker returns is therefore less trusted than the
parent that reads it.

## What happens

Reproduction: a fake raster worker returns a path inside its own output directory holding
roughly 1 MiB of `NOT_A_PNG` bytes, while the transform is configured with `max_page_bytes: 64`.
The transform succeeds, persists the full payload, and labels it `image/png`.

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

## Where to start

One file and, in effect, one function: `_map_rasterize_response()` in
`src/elspeth/plugins/transforms/pdf_rasterize.py`. The configuration field (`max_page_bytes`)
and the typed page-refusal machinery the fix should reuse — including the `pdf_page_too_large`
refusal reason for oversized pages — are already in that same module.

**Size.** Small, self-contained and a reasonable first issue: no design decision is
outstanding, the correct behaviour is agreed, the configuration field already exists, and the
reproduction above converts directly into the regression test.

## Impact

A defective or compromised worker can exceed the byte cap, cause an unbounded parent-side read,
and publish arbitrary bytes downstream under a false MIME type — anything consuming the output
believes it is handling a PNG within the configured size. The containment check does limit
*which* file can be read, so this is not an arbitrary-file-read: the escape is in size and
content, not in location.

## Fix

The parent enforces the actual output size and the PNG content contract before storage: check
the on-disk size against `max_page_bytes` before reading the file (or read bounded), verify the
PNG signature, and treat a violation as one of the typed page refusals the transform already
has for oversized pages rather than persisting the payload.

You would know it holds with two tests, not one. The reproduction above must fail the document
or refuse the page instead of succeeding — and a worker returning a valid PNG comfortably under
the cap must still succeed, so the new check is proven to be discriminating rather than simply
rejecting everything.
