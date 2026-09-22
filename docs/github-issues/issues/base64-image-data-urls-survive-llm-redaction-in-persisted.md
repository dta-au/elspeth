---
title: Base64 image data URLs survive LLM redaction in persisted HTTP audit payloads
labels: [area/audit, area/plugins, type/bug]
---

Image bytes that the logical LLM audit record redacts are still written verbatim into the underlying HTTP call payload, so confidential image contents reach durable audit storage contrary to the bytes-free LLM audit contract.

## What happens

Requests that carry images through the OpenRouter and Gateway providers embed the image bytes as `data:image/...;base64,...` URLs in the message content. The logical LLM audit record applies the bytes-free redaction policy. The audited HTTP request that actually carries the call persists the complete request body, including the exact base64 data URL, as a `CallType.HTTP` payload.

Reported reproduction: issue an image-bearing request through either provider and inspect the persisted call rows. The logical LLM record is redacted while the corresponding HTTP request payload still contains the data URL. Observed during an aggregate compatibility review at `3dc67fb1d`.

## Why

The HTTP call recorder stores the request body it was given, with no content-aware filtering. `_prepare_call_payloads` in `src/elspeth/core/landscape/execution/calls.py:271-310` converts the request payload to a dict, hashes it, and serialises the whole dict with `canonical_json` into the bytes staged for the payload store. Whatever is in the request body arrives in durable storage unchanged, so a redaction applied only to the logical LLM record does not reach it.

## Note on the provider citations

The original review cited `src/elspeth/plugins/llm/providers/openrouter.py:353` and `.../gateway.py:491-493`. Those modules now live under `src/elspeth/plugins/transforms/llm/providers/`, and the line numbers no longer hold. More usefully, `grep -rn "data:image" src/elspeth --include=*.py` returns no match anywhere in the Python source, so no provider constructs an image data URL: they appear to arrive in caller-supplied message content and are carried through. Treat the provider references as pointing at the request path, not at the embedding site.

## Impact

Confidential image contents and large binary payloads are duplicated into durable audit storage. The blast radius is limited to image-bearing requests through these two providers, but every such request is affected, and the storage is durable and intended to be retained.

## Fix

Apply one consistent audit policy across the logical LLM record and the underlying HTTP record. Strip, externalise, or hash image bytes before persistence while retaining enough request provenance for the audit trail to remain useful.

Done looks like: an image-bearing request through either provider leaves no base64 payload in any persisted call row, and the check is made by inspecting the stored rows rather than by reading the redaction code.
