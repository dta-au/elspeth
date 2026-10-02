# Composer chat-card repairs

Repairs for the eight findings from the 2026-09-22 chat-card lifecycle review,
based on release commit `7d981d6cf7c26c434085c084790db9fbc1da5d79`.
Tracker: `elspeth-9c5780d150`.

## Resulting behavior

1. Prompt approval requires the complete live prompt, not the bounded event
   preview. Opening a preview while composition loads does not authorize an
   unseen complete prompt. Full content arriving into an open disclosure is
   displayed and becomes reviewable. Separate interpretation-slot attestation
   keeps its existing semantics.
2. Inspection cards confirm the source's actual headers. The misleading
   rename/remove editor is removed; column changes belong in a processing step
   requested through Composer chat. The backend rejects altered confirmation
   headers, including from older clients. The provider remains responsible for
   authoring transformations.
3. A typed stale-base refusal disables proposal acceptance while retaining
   Reject and a rebase explanation. Contention stays retryable. A base mismatch
   alone suppresses the misleading current-state diff; server acceptance remains
   authoritative because durable blob-effect retries have a valid exception.
4. Proposal snapshots preserve pending arrivals after read dispatch and terminal
   receipts. Older reads cannot replace a newer list. Accept hydration also
   preserves composition published by a subsequent compose operation.
5. Source summaries and Edit actions are bound to active blob identities. Failed
   replacement hydration cannot leave an obsolete source editable. Failures show
   Retry source details while retaining successful current summaries.
6. All assistant-created source blobs can have cards, regardless of uploaded
   sources sorting before them. Cards and audit labels follow named-source order;
   Edit identifies the source names and blob, including duplicate filenames.
7. Guided JSON fields retain raw editor text independently of parsed values.
   Invalid syntax and wrong containers block submission. Valid scalar strings,
   conditionals, and optional-null behavior remain supported.
8. Recovery dialogs explain unsaved partial drafts inside the dialog, omit
   impossible Apply actions, and focus Discard. Saved-draft recovery is retained.

## Debugging and review

Each lane started with a reproduced failure and a working control, promoted
regressions into repository tests, observed failures before the corresponding
production change, and then verified the fix. Independent review additionally
reproduced two interactions in the proposal repair: late Accept hydration could
roll back the displayed composition; a busy Reject could erase previously
confirmed stale-base evidence. Both belong to the same state-ownership boundary.

Source review strengthened tests for session switches, selected-card Edit
payloads, partial hydration failure, retry, and deterministic multi-source order.
Prompt/recovery review found no remaining blocking issue in the inspected paths.

Detailed lane notes and red/green logs are local support artifacts under
`.claude/lanes/chat-card-repairs/` and `/tmp/`; regression tests live with the
production components and backend transition/route tests.

## Validation

Combined frontend validation ran on a frozen tree (before/after digest
`71e97e410497aa6bacb5bdb27a5b9f2c03083ead633b3acc27ad8877fae5b3ed`):

- Full frontend suite: **5,022 passed across 278 files**, exit 0.
- Typecheck, full ESLint, and production build: each exit 0.
- Guided authoring, proposal routes, blob-retry controls and attribute contracts:
  **1,662 passed**, exit 0. One upstream Pydantic warning.
- Changed Python files: Ruff, format check and production-file mypy passed.
- Contract alignment and soft-mapping census: exit 0.
- Whole-tree mock-discipline and dynamic-attribute baseline gates: **247 passed**,
  exit 0.
- All-rule static-analysis corpus: **2,257 before and after**, no added or removed
  findings after normalizing source line positions. Both runs exit 1 for the
  existing fail-closed corpus; this is an unchanged baseline, not a green gate.

Frontend logs and frozen-tree summary are in `/tmp/chat-card-final-verification/`.
After that run, commit review required replacing a pre-existing user-home test
fixture path with a portable data path and rewording its companion comment.
The affected SchemaFormTurn suite passed again (**71 tests**, exit 0), with scoped
ESLint exit 0. No executable production behavior changed in that cleanup.
The completed backend log is `/tmp/chat-cards-python-verified.log`. An earlier
sandboxed backend run stalled and was interrupted (exit 2); it is not the passing
evidence. The same bounded selection completed outside the sandbox.

The Python scope is guided authoring, proposal routes and blob-retry controls,
plus affected whole-tree checks. No schema, database mutation, locking, or pipeline
execution implementation changes are included, so the full Python and PostgreSQL
suites were not rerun. Live production reproduction and deployment are separate
from these deterministic tests. Operator signing remains deferred; no signatures
were edited.
