---
title: Frontend API client trusts most responses through an unchecked cast
labels: [area/web, type/task, priority/P3]
---

The web frontend's API client has two ways of turning a response body into a typed value: a
structural decoder that checks the shape and fails closed, and a bare type assertion that
checks nothing. Thirteen call sites use the first; about sixty-three use the second.

## Where to start

`src/elspeth/web/frontend/src/api/client.ts`. The shared helper is `parseResponse<T>` at
`:243`. The two forms sit side by side in the same file.

## Why

Measured in that file: `parseResponse<` appears 77 times, one of which is the definition, so
there are 76 call sites. Thirteen of them use `parseResponse<unknown>` and hand the result to a
decoder — for example `:828` and `:840`, which pass theirs to `decodeUserComposerPreferences`.
The remaining sites name a type in the call and accept whatever arrives as that type:

```ts
export async function listSecrets(): Promise<SecretInventoryItem[]> {
  const response = await authFetch("/api/secrets", { headers: authHeaders() });
  return parseResponse<SecretInventoryItem[]>(response);
}
```

`listSecrets` at `:1735` and `getBlobMetadata` at `:1638` are two of them. A drifted or
malformed body is accepted without complaint and flows on as a value the rest of the
application treats as validated.

## Impact

Not a disclosure or privilege problem, and it should not be read as one. The server remains the
authority for what any caller may do, and these endpoints return metadata — the secrets
endpoint returns references and never values. What the missing check costs is diagnosability
and robustness: a field that is absent or the wrong type produces an error somewhere downstream
in a render path, with nothing tying it back to the response that caused it. A version skew
between a deployed backend and a cached frontend bundle is the realistic way to hit it.

## Fix

The wrong move is to fix one call site, and the second wrong move is to copy the decoder
primitives a fifth time. They are already duplicated across four files —
`src/elspeth/web/frontend/src/api/guidedDecoder.ts`,
`src/elspeth/web/frontend/src/api/preferencesDecoder.ts`,
`src/elspeth/web/frontend/src/api/auditReadiness.ts` and
`src/elspeth/web/frontend/src/api/shareableReviews.ts` — and the four copies disagree: the two
under a decoder name throw a plain `Error` with a message prefix hardcoded into the primitive,
while the two others throw an `ApiError`.

So the sequence is:

1. **Settle the error contract and extract a shared leaf**, with the message prefix passed in
   rather than baked in, and all four existing copies importing it. That work is open
   separately and is the natural prerequisite; a contributor wanting this issue should expect
   to start there.
2. **Convert the call sites**, beginning with the payloads that cross a version boundary or
   feed a user-visible surface directly.

You would know it holds when a bare type argument to `parseResponse` no longer appears outside
a named and justified set, when a malformed body produces a decoder error naming the offending
field rather than a downstream failure, and when a lint rule stops a new bare cast being added.

**Size.** Large in aggregate, but the work divides cleanly. The leaf module and the error-type
decision are the substance; each converted call site after that is mechanical and reviewable on
its own. Not a first issue, but a good second one.
