# Authenticated web fetch response evidence gate

**Status:** Authenticated execution is disabled. The `web_scrape.auth` contract accepts a
secret reference for Basic, Bearer, or header API-key authentication and binds
it to one HTTPS origin. Runtime execution refuses every authenticated row
before DNS or HTTP until the response evidence boundary below is implemented.

## Why the gate is necessary

`AuditedHTTPClient.request_ssrf_safe()` records response bodies before
`web_scrape` extracts content. The resulting raw response evidence and
processed row can be read through normal audit, replay, and output paths. An
authenticated remote endpoint can echo an Authorization or API-key header in
its body, including after encoding or transformation. Checking for the exact
credential string or wire header before recording catches only direct echoes;
it cannot establish that the persisted body is free of credential material.
The same issue applies to redirect responses and error bodies.

## Existing storage and disclosure paths (source review, 2026-09-29)

| Boundary | Current behavior | Consequence for authenticated fetch |
| --- | --- | --- |
| HTTP call evidence | `AuditedHTTPClient._build_response_payload()` places parsed `body` and a base64 copy of the exact body in `HTTPCallResponse.transport`; `_restore_replay_response()` reads that transport. Redirect hops also retain exact transport. | An echo can be stored twice, including in a redirect or error response. Removing transport alone breaks current replay. |
| Landscape call and row payloads | `CallAuditRepository._prepare_call_payloads()` serializes the response into the ordinary `PayloadStore`; token creation stores canonical row data there too. `web_scrape` additionally stores extracted content and puts it in the output row. | Protecting only the HTTP response leaves the same bytes, or a transformation of them, in other ordinary payloads and sinks. |
| Payload store | `PayloadStore.store(bytes) -> SHA-256` has no classification or principal argument. `FilesystemPayloadStore` writes the supplied bytes and returns them by hash. `SourceBoundPayloadStore` copies replay outputs into the current ordinary store. | The supported store offers integrity and retention, but no confidential compartment or authorization on read and replay. |
| Existing encryption | `UserSecretStore` encrypts user secret *values* in the session database. Optional SQLCipher encrypts the Landscape SQLite database file. | Neither encrypts filesystem payload blobs or controls their readers and downstream publication. |
| Web and error projections | Web output preview/download reads sink artifacts; execution diagnostics intentionally omit raw `row_data_json`, while ordinary audit and sink records still retain it. The exception scrubber is pattern based and explicitly does not cover arbitrary payloads. | A diagnostic projection's restraint does not protect stored responses, output artifacts, CLI audit readers, or a transformed echo in an error message. |

These are current code paths, not a supported confidential-response facility. In
particular, encrypting one blob in place would leave the parsed response, row,
checkpoint, operation output, sink artifact, export, or replay copy exposed.

## Missing primitive and release design

The missing primitive is an **end-to-end classified data path**, carried from
the first response byte through call evidence, replay, extraction, row and
checkpoint persistence, errors, sinks, exports, and every Web/CLI reader. A
response marked confidential must not be accepted by an unclassified
`PayloadStore.store()` or converted to an ordinary `PipelineRow`. An opaque,
authenticated-encrypted evidence store should bind ciphertext to run, call,
origin, credential reference/version, response status, and ordered redirect
hop. Its opaque reference and keyed integrity commitment may be audited, but the body,
response headers that can carry secrets, and derived values require a
separate read capability. Replay can use that capability offline; verify
must compare classified evidence without publishing decrypted bytes. Missing
keys, labels, refs, or authorization must abort before output publication.

Classification must propagate conservatively to *every* value derived from a
confidential response, including extraction results, fingerprints, schema
errors, and model prompts. A bounded, operator-controlled declassification
operation is required before any derived field can reach an ordinary sink or
Composer context. It must record the exact fields, source origin, data
contract, recipient, and reason, and apply the same decision in replay and
verify. Ordinary string redaction, a CSS selector, or a field allowlist cannot
prove the selected value is free of a transformed credential echo.

There is a hard limit to the guarantee: an endpoint that knows the credential
and controls arbitrary response values can encode it in any published value.
Even a boolean repeated across a batch can carry bits. No generic parser can
prove a useful arbitrary endpoint result independent of that credential. A
release policy must therefore either keep all response-derived data
confidential, or explicitly trust the endpoint and approve a narrow
declassification contract. The latter is an operator trust decision, not a
technical proof that transformed echoes are impossible. Until that policy and
classified path exist, the current runtime refusal is the correct behavior.

## Required release boundary

1. Define an operator-approved retention and access policy for authenticated
   response bodies and derived rows. The response and any body-bearing error
   must be classified before audit persistence. Raw or derived credential
   material must not enter ordinary Landscape payloads, telemetry, logs,
   user-visible error text, Composer context, or exported rows.
2. Provide a protected response artifact path with encryption, access control,
   and retention enforcement, or an equally strong extraction boundary that
   proves only nonsecret fields enter ordinary evidence. Merely scanning for a
   direct echo is insufficient.
3. Preserve replay and verify semantics: replay consumes the admitted archived
   response without network access or a live credential; verify checks the
   authenticated request's scheme, exact origin, secret reference identity and
   version/fingerprint, request body, and every redirect hop before dispatch.
   Secret values remain absent from compared or operator-visible records.
4. Keep Basic, Bearer, and API-key credentials out of authored literal YAML and
   row values. Resolve web-authored markers through the existing secret
   inventory and authorization gate. CLI settings accept only an exact
   environment secret reference for `auth.credential`, with no default value.
5. Make the request path deny initial and redirect destinations outside the
   configured HTTPS auth origin before DNS or dispatch, including replayed
   redirect evidence. No cross-row cookie jar or authenticated cache is in
   scope for this candidate.

## Acceptance tests before opening the gate

- For all three schemes, a local fixture receives the expected wire header;
  request audit, telemetry, logs, errors, and exports contain only safe
  identity evidence. A custom API-key header that does not look sensitive by
  name must be HMAC fingerprinted.
- Direct, encoded, transformed, and error-response echoes of the credential
  remain inaccessible through ordinary audit, output, and Composer surfaces.
  A test that merely searches for the exact token does not satisfy this gate.
- A row URL, HTTPS-to-HTTP downgrade, alternate port, and cross-origin
  redirect are refused before the unauthorized host is resolved or contacted.
  Same-origin redirects retain request identity and admit each hop.
- Replay is offline and does not require a live credential. Missing, tampered,
  or differently scoped credential evidence and redirect hops fail closed.
  Verify refuses mismatched request or response evidence before publication.
- The configured auth mode stays unavailable in the catalog until these tests
  pass against the integrated runtime. The current candidate's runtime denial
  test proves no authenticated request leaves the process.

The classified path additionally needs negative controls that intentionally
put raw, base64, URL-encoded, hash-derived, split-across-fields, and
batch-bit-encoded credential material in response bodies, response headers,
redirects, extraction errors, and sink rows. Tests must try each ordinary
audit, export, preview, download, telemetry, log, Composer, and CLI reader;
they must see only classified references or deny access. Replay must work
without the live credential when granted the classified evidence capability,
and fail closed without that capability. A declassification test must prove
the declared trust policy and field scope are enforced, while recording that
such a policy cannot establish secrecy against an adversarial endpoint.
