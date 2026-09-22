---
title: Base64 image data URLs survive LLM redaction in persisted HTTP audit payloads
labels: [area/audit, area/plugins, type/bug]
---

Image bytes that the LLM audit record deliberately strips are still written verbatim into the HTTP call record underneath it, so image contents reach durable audit storage against the policy that exists to keep them out.

**Where this lives.** Landscape is ELSPETH's audit trail — every LLM call and every HTTP request is recorded there. One image-bearing LLM call produces two records: the logical LLM call, and the HTTP request that carried it. The policy is in `src/elspeth/contracts/chat_parts.py`, which defines the bytes-free projection as "the ONLY image shape audit may hold" (line 92); the LLM client applies it at `src/elspeth/plugins/infrastructure/clients/llm.py:484` (`messages=audit_messages(messages)`), and its own docstring states the intended split: "The SDK sees the wire projection (base64 image data URIs); the audit trail sees the bytes-free projection" (lines 447-450).

## What happens

Requests carrying images through the OpenRouter and Gateway providers embed the image bytes as `data:image/...;base64,...` URLs in the message content. The logical LLM record gets the bytes-free projection as designed. The HTTP request record does not: it persists the complete request body, data URL included, as a `CallType.HTTP` payload.

Reported reproduction: issue an image-bearing request through either provider and inspect the persisted call rows. The logical LLM record is redacted while the corresponding HTTP request payload still holds the exact data URL. Observed during a compatibility review at `3dc67fb1d`.

## Why

The HTTP call recorder stores the request body it is given, with no content-aware filtering. `_prepare_call_payloads` in `src/elspeth/core/landscape/execution/calls.py:271-310` converts the request payload to a dict, hashes it, and serialises the whole dict with `canonical_json` into the bytes staged for the payload store. Whatever is in the body arrives in storage unchanged. The bytes-free projection is applied one layer above, so it never reaches this path.

## Note on the provider citations

The review cited `src/elspeth/plugins/transforms/llm/providers/openrouter.py:353` and `.../gateway.py:491-493`. Those modules now live under `src/elspeth/plugins/transforms/llm/providers/` and the line numbers no longer hold. More usefully, `grep -rn "data:image" src/elspeth --include=*.py` returns no match anywhere in the Python source, so no provider constructs an image data URL: they arrive in caller-supplied message content and are carried through. Treat the provider references as naming the request path, not the place the bytes are introduced.

## Impact

Image contents and large binary payloads are duplicated into durable audit storage. The blast radius is limited to image-bearing requests through these two providers, but every such request is affected, and audit storage is meant to be retained.

## Fix

Correct behaviour: one audit policy applied to both records of the same call. The HTTP payload for an image-bearing request holds no base64 image data, while still carrying enough of the request to be worth auditing — which provider, which model, which message structure, and a stable reference to the image (a hash, or a blob reference, as `chat_parts.py` already does for the LLM record).

The first decision is which of strip, externalise or hash applies here, and whether the HTTP recorder should apply it itself or be handed an already-projected body by the layer above. The second option keeps one definition of the policy but means the HTTP client's caller has to supply an audit form distinct from the wire form; the first puts content knowledge into a transport-level recorder that currently has none. That trade-off is not settled in the source.

You would know it holds by making an image-bearing request through each provider and reading the stored call rows — both of them — confirming no base64 payload is present and that the surviving reference still identifies the image. Check the rows, not the redaction code: the whole defect is that a correct-looking redaction did not reach the second record.

Size: the change itself is small, but it is design-blocked. Settle the strip/externalise/hash question and the layering question first; the code follows quickly once they are answered.
