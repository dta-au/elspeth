# LLM image input — design

Date: 2026-08-25 · Branch: worktree-llm-vision-input (base: feature/unified-lineage @ a0af68914)
Status: DESIGN — approved in brainstorm, pending implementation plan

## 1. Problem and driving use case

Every layer of the LLM plugin stack is text-only: `LLMProvider.execute_query` and all four
providers type messages as `list[dict[str, str]]` (`plugins/transforms/llm/provider.py:293`,
`providers/{azure,bedrock,openrouter,gateway}.py`), the base client agrees
(`plugins/infrastructure/clients/llm.py:356`), transform strategies build only
`{"role": "user", "content": rendered_prompt}` from a Jinja-rendered string, and audit
records validate `content` as `str`. Vision-capable models cannot be used.

Driving use case: the `pdf_rasterize` transform (branch `feat/pdf-rasterize`) explodes a PDF
into one row per page, each carrying `page_blob_ref` (payload-store SHA-256 content hash of
the PNG page), `page_number`, `document_id`, `page_mime_type` (`image/png`), and size/width/
height fields. Those rows feed an LLM that extracts information into a typed output template.

## 2. Decisions (recorded from brainstorm)

| Question | Decision |
|---|---|
| How rows reference images | Existing blob path: config names blob-ref column(s); bytes resolved via `ctx.payload_store` |
| Format scope | `jpeg`/`png` now; contract designed so `pdf` is a later additive literal |
| Provider scope | All four (azure, bedrock, openrouter, gateway) in one parity sweep |
| Audit shape | Hash + blob ref only — bytes never enter audit |
| Prompt composition | Config-declared columns; parts appended after rendered text. Jinja stays text-only |
| Seam design | Approach A: owned typed content-part model end-to-end (ADR-032-aligned) |
| Output schema | Reuse `llm_multi_query`'s existing `output_fields`/`ResponseFormat`; no new machinery |
| Scope boundary | LLM side only. `pdf_rasterize` and any explode/stitch engine work are separate |

## 3. Content-part contracts

New module `src/elspeth/contracts/chat_parts.py` (L0, no upward imports, sibling of
`binary_documents.py`):

- `ImageFormat = Literal["jpeg", "png"]` — subset of `BinaryDocumentFormat`. Widening to
  `pdf` later is one additive literal plus provider verification.
- `TextPart(text: str)` — frozen dataclass.
- `ImagePart(format: ImageFormat, data: bytes, sha256: str, byte_count: int, blob_ref: str | None)`
  — frozen. Constructed only via `ImagePart.from_bytes(format=, data=, blob_ref=)`, which
  computes `sha256`/`byte_count` and verifies the byte signature against the declared format
  with `binary_document_signature_matches`. Per the `binary_documents.py` doctrine the
  signature proves agreement with a declared format; it never chooses one. `__post_init__`
  re-asserts all invariants (signature, hash, count) so a hand-built instance cannot lie.
- `ContentPart = TextPart | ImagePart`.
- `ChatMessage(role: Literal["system", "user", "assistant"], content: str | tuple[ContentPart, ...])`.
  Content stays a plain `str` when no images are involved: text-only pipelines produce
  byte-identical audit records to today.
- `ImagePartAudit(format, sha256, byte_count, blob_ref)` — the bytes-free projection, built
  by `ImagePart.audit_view()`. The only image representation permitted in audit, tracing,
  hashing, and logs.

The seam widens in one parity sweep: `LLMProvider` protocol, all four providers, and
`chat_completion` change from `list[dict[str, str]]` to `Sequence[ChatMessage]`. No
half-widened state is left behind (shared-invariant doctrine: a shared contract must not
live inside one specialized component).

## 4. Transform config and message binding

`LLMConfig` (shared by `llm` and `llm_multi_query`) gains:

```yaml
image_inputs:                 # optional; absent = exactly today's text-only behavior
  - field: page_blob_ref      # blob-ref column: str or list[str] per row
    format: png               # EITHER a literal ...
    # format_field: page_mime_type   # ... OR a column (mime mapped via
    #                                # BINARY_DOCUMENT_FORMAT_BY_MIME); exactly one required
    required: true            # false -> null/absent column contributes no parts
max_image_bytes: 5242880      # per image; hard upper bound 20 MiB
max_images_per_call: 20       # exceeding -> row-level error, routed on_error
```

Binding happens in shared message assembly (used by both `SingleQueryStrategy` and
`MultiQueryStrategy`), after template render:

1. Read each declared column; for list-valued columns preserve list order.
2. Resolve each ref through `ctx.payload_store` — same pattern as
   `textract_inline_analysis._read_document_bytes` (`PayloadNotFoundError`, size cap,
   signature check all yield row-level error outcomes).
3. Build `ImagePart.from_bytes(...)` per image; assemble
   `content = (TextPart(rendered_prompt), *image_parts)` in config order, then per-row list
   order. System prompt remains a plain string message.

This consumes `pdf_rasterize` rows directly (`field: page_blob_ref`,
`format_field: page_mime_type`); a stitched multi-page row with a list-valued ref column
works unchanged.

Config validation (fail-fast at build): exactly one of `format`/`format_field`; declared
fields join `declared_input_fields`; identifier and collision checks per house pattern;
`max_image_bytes` within (0, 20 MiB]; `image_inputs` entries non-empty and distinct.

Jinja templates remain text-only. Interleaved text/image placement is out of scope; the
ordered-parts tuple makes it additive later without reshaping the contract.

## 5. Providers

One shared serializer, `plugins/transforms/llm/providers/_content_parts.py`:
`serialize_parts(content: str | tuple[ContentPart, ...]) -> str | list[dict[str, Any]]`.
A `str` passes through untouched; parts become the OpenAI content-parts array
(`{"type": "text", "text": ...}` /
`{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}`).

All four providers call it at wire-build time:

- **azure** — base client wraps `openai.OpenAI`/`AzureOpenAI`; the SDK accepts the array.
- **bedrock** — litellm accepts the same OpenAI shape and translates to Converse/Anthropic
  blocks itself.
- **openrouter / gateway** — direct HTTP JSON; the `"messages"` payload takes the array.

No per-provider merge logic exists: the parity surface is one serializer plus four thin
call sites.

`runtime_preflight` stays text-only. A non-vision model rejecting an image-bearing call
surfaces as a typed, non-retryable provider error routed row-level. A `model_catalog`
vision-capability flag is a possible follow-up, explicitly out of scope here.

## 6. Audit, hashing, replay, tracing

- **Bytes never cross into audit.** Before recording, messages are projected through
  `audit_view()`: each image part appears in the recorded `LLMCallRequest.messages`
  (already `Sequence[Mapping[str, Any]]`, `contracts/call_data.py:142`) as
  `{format, sha256, byte_count, blob_ref}`.
- Existing text hashes (`template_hash`, `variables_hash`, `rendered_hash`) are untouched.
- New `parts_hash = sha256(canonical_json(ordered audit views))`, recorded when images are
  present and folded into request fingerprinting — replay/verification binds to exact bytes
  (sha256 + payload-store ref) without storing them.
- Text-only calls produce byte-identical audit records to the pre-change tree.
- Langfuse/tracing render placeholders (`[image png 1.2MB sha256:ab12…]`), never payloads.
- Secret-scrub applies to text parts only; image parts are structurally exempt because no
  byte payload ever reaches a scrubbed surface.

## 7. Error handling

Row-level outcomes (each a distinct typed `reason`, routed `on_error`, following
`textract_inline_analysis` vocabulary): missing blob (`PayloadNotFoundError`); null value
in a `required` column; `max_image_bytes` exceeded; signature/format mismatch;
`format_field` mime not in `BINARY_DOCUMENT_FORMAT_BY_MIME`; `max_images_per_call`
exceeded; provider rejection of image content (non-retryable `LLMClientError`).
`ContentPolicyError`/retry semantics are unchanged. Nothing new aborts the run.

## 8. Output schema (structured extraction)

Reuse, don't invent. `llm_multi_query` already provides typed `output_fields`
(`multi_query.py`), two enforcement modes (`ResponseFormat.STRUCTURED` → API-native
`json_schema` response_format; `STANDARD` → `json_object` + canonical schema appended to
the prompt, `transform.py:576-604`), and runtime validation into per-field row outputs.

- Because `image_inputs` binds at the shared message-assembly seam, `llm_multi_query`
  gains images for free. The driving use case is one query entry: rendered prompt + page
  images in, `output_fields` with `response_format: structured` out.
- No `output_fields` port to the single-query `llm` transform in this change — that would
  duplicate the structured-output contract on a second surface. If parity matters later it
  is its own parity-sweep ticket.
- Pinned interaction: `response_format=json_schema` AND image parts on the same call, per
  provider (serializer golden + one integration case each).

## 9. Testing

- **Unit:** `ImagePart.from_bytes` invariants — signature mismatch, hash/count tampering
  (mutation-test the guard, not just the defect); config validation matrix; serializer
  goldens (exact wire JSON per part shape); per-provider call-site pass-through.
- **Integration:** transform + fake provider capturing `ChatMessage`s — part ordering
  (text first, config order, row-list order), list-valued columns, `required: false`,
  every §7 reason and its `on_error` routing; json_schema + images together.
- **E2E:** `blob_rows` source with PNG fixtures → LLM transform with stub provider →
  assert audit.db holds hash+ref and zero image bytes, `parts_hash` recorded, and a
  text-only pipeline's audit records are byte-identical to pre-change.
- **Whole-tree gates:** knob-schema golden for the widened `LLMConfig`, catalog/discovery
  counts, state-engine plugin matrix, wardline scan (gate-of-record invocation), trust-tier
  corpus compare (count before/after, never tail), full `pytest tests/`. Add the new seam
  to `docs/agents/recent-code-hints.md` in the same commit that lands it.

## 10. Out of scope

- The `pdf_rasterize` transform and explode/stitch engine work (separate branch/spec).
- PDF-as-LLM-input (`pdf` literal widening) and template-controlled part interleaving —
  both designed to be additive.
- `output_fields` on the single-query `llm` transform.
- `model_catalog` vision-capability flag / preflight vision detection.
- Web Composer authoring surfaces for `image_inputs` (schema exposure follows the normal
  catalog path; no composer-special handling — and no tutorial-special paths, ever).
