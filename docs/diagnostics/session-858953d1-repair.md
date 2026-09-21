# Composer session 858953d1: recovered tool failures and incorrect diagnostics

Session: `858953d1-fd91-42a1-817e-7cf8bb7951c3`, observed 2026-09-21.
The saved conversation and execution audit establish recoverable authoring
failures and incorrect explanations. They do not establish a stalled run.

## Observed defects

- The planner requested an invented-source review after that requirement was
  resolved. The tool correctly rejected the request for a missing pending site.
- A later `set_pipeline` call failed the provider argument-envelope guard. The
  discovery tool described a flat authoring document as exact call arguments,
  although the web tool requires a single `pipeline` wrapper. The persisted
  diagnostic omitted the required shape.
- A cleanup mapping reversed the field mapper's input-to-output direction.
- The reference-join contract preview tried to construct a plugin with an
  unresolved inline-content blob marker. It reported a high-severity failure
  and claimed rejection even though authoritative materialized validation and
  execution succeeded. The planner incorrectly attributed this to upstream
  LLM nondeterminism and attempted unrelated option changes.
- Approved profile-bound LLM nodes lost their validation-only provider binding:
  the approval artifact hash was incorrectly treated as a private binding
  conflict. This produced additional false contract-probe warnings.
- The A/B reconstruction capitalized supplied literal system prompts and used
  structured enum output for the skilled arm but free text for the control.
  The planner nevertheless claimed the system prompt was the only difference.
- A subsequent prompt edit inherited the old approval hash and reached plugin
  validation before the review reconciler could invalidate that approval. The
  tool rejected the edit, causing an unnecessary rebuild and another retry.

## Runtime evidence

The session run API recorded four completed runs, all with six source rows and
zero failed tokens, at composition versions 5, 10, 19, and 23. The two forked
runs recorded 24 terminal tokens: 18 successful and 6 structural. Token totals
are not output-row counts.

For runs `db1dfac9-30af-4b51-9214-f9b6d391bfe6` (v19) and
`d4ebb50f-41c1-4e9a-945e-fc9198407482` (v23), the Landscape `calls` records
contained 12 audited HTTP requests and 12 logical LLM requests per run.
Request and response payloads were read from the configured payload store and
verified against their SHA256 content references before inspection.

Both arms sent a system-role message on every sampled HTTP request to
`anthropic/claude-sonnet-4.6`, at temperature 0. The recorded text was:

```text
You are a skilled categorisation subagent
You return a single random word instead of what was asked for
```

The application therefore did not drop the system prompt on those runs. The
capitalization differs from the user's supplied lowercase literals. The v19
control responses were category words; this alone does not establish why the
model returned them. Configuration, outbound messages, and model compliance
are separate evidence questions.

The user subsequently added `you return plausible but incorrect answers always`
to the control's user prompt. Any later live repair must preserve that newer
intent, and must not describe the resulting experiment as changing only the
system prompt.

## Repair boundaries

The repair clarifies the web envelope, preserves the precise safe envelope
diagnostic in the audit, and teaches literal preservation, experimental
controls, field mapping direction, current review eligibility, and
evidence-based diagnosis. The LLM remains responsible for authoring the graph.

Resolver-free contract probing retains successful constructor results. Only
an expected construction failure caused solely by a valid unresolved
inline-content string value is classified as deferred. It yields no proven
output guarantees. Malformed markers, unknown options, mixed invalid options,
and invalid approval digests remain failures. Actual execution admission still
requires authorized blob resolution and validation of the materialized bytes.

Prompt patches now reconcile an in-memory candidate before validating its
plugin options. Stored approval evidence is checked first; rejected edits still
return the original state. Structured prompts must be edited through
`prompt_template_parts`, preserving interpretation references. Existing
context-bound definition approvals still reopen when their surrounding prompt
changes; this protection was deliberately retained.

## Verification scope

Focused regressions include negative controls restoring the original defects,
malformed and unauthorized content, mixed invalid options, and successful
construction with an unused marker. Prompt-content tests prove that corrected
instructions reach the planner; they do not prove model adherence. Package gate
results and live acceptance must be recorded separately after completion.
