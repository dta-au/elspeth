# Composer graph recovery (#298)

Implement the reviewed 0.8.5 requirement: the provider can end any authoring
turn by parking a graph, resetting to the exact turn start or the latest safe
editable checkpoint, or restoring an explicitly selected parked graph. The
same response carries its message to the user. Accepted controls cannot
re-enter the provider, advisor or repair loop.

The isolated implementation starts at `e6554404344d47ad94567f395339b02b4c13dd31`
(the committed #297 fixes). This is an older emergency precursor, not the final
PR #296 candidate. Keep this delta separate; reconcile the final emergency/main
baseline and #297 with the release integrator before integration.

The recovery schema uses epoch 74 for graph parks and operation-bound
checkpoints. Ambiguous epoch 73 is retired; the release integrator reserves
75 or a newer unused epoch for the later auth/recovery union. Neither branch's
local checks qualify the combined release schema. John retained the temporary
no-backwards-compatibility window on 12 October 2026, without an exact expiry.
Qualification uses new compatible stores. Unsupported stores must be refused
before incompatible writes, with preservation guidance; no upgrade project,
store conversion, reset or recreation is authorized. Do not import held
PR #287 wholesale to reconcile it.

## Contract

- Checkpoint eligibility means editability, not execution readiness. Use
  bounded local canonical-shape, identity, namespace and intrinsic editor
  checks, without plugin construction, resource access or runtime preflight.
  Incomplete topology and missing execution prerequisites remain editable.
- `park` stores an immutable session-owned snapshot and leaves graph content
  unchanged. Claim retention only after successful persistence.
- `reset_to_turn_start` identifies the exact operation-bound starting graph.
  An unavailable target fails honestly without substitution.
- `reset_to_safe_checkpoint` deterministically selects the latest eligible
  state at/before turn start or subsequently committed by this operation.
  No eligible state means an explicitly disclosed empty authoring workspace.
- `restore_parked_graph` resolves an explicit session-owned park ID. Missing
  or malformed targets fail without substitution.
- Saving, serializing, validating, hashing or capturing the displaced broken
  draft is never a prerequisite for reset. Existing history and evidence stay.
- Malformed durable heads use admission before graph/gate hydration, based
  on trusted scalar identity and authority. The provider sees safe recovery
  facts, not a fabricated current graph or the malformed payload.
- Recovery appends a new version under expected-head and epoch fences, with
  the receipt, assistant message and operation completion in one transaction.
  Replay returns the same result. Newer work cannot be silently overwritten.
- Historical approvals do not become current authorization. Uploads, history
  and external effects are not rolled back. Persistence/provider failures do
  not become recovery-success claims.

## Implementation and verification

1. Add shared nominal intent/context contracts and pure local qualification;
   test incomplete drafts, malformed structures and no plugin/resource access.
2. Add immutable park persistence, scalar admission, deterministic target
   selection and fenced atomic settlement; test SQLite and PostgreSQL failure,
   contention and replay paths.
3. Integrate terminal controls in ordinary, planner, repair, hatch and reply-only
   palettes. Reject mixed mutation/control batches before dispatch. Use scripted
   provider responses to establish zero calls after acceptance.
4. Integrate pre-hydration route admission, truthful terminal response and UI
   observation. Retain ordinary authoring and execution authority boundaries.
5. Reuse independent LLM, security and correctness/systems reviewers against
   the implementation and adversarial tests. Address their material findings.
6. Run affected tests, whole-tree contracts and source gates, lint/type checks,
   keyless finding-set comparison and PostgreSQL qualification. Coordinate host
   capacity before the required frozen broad suite; emergency CI has priority.
7. Admit all configured local stores read-only before bootstrap. Require exact
   current schemas or pristine unstamped allocations; preserve unsupported
   session, Landscape and credential stores, and retain secure file admission.
   Session engine and local-auth construction validate before journal/schema
   writes. Landscape admission uses `LandscapeDB.from_url`; the legacy direct
   `LandscapeDB` constructor is outside this preservation guarantee.

No live provider calls are authorized. Tests use owned scripted fakes. The
shared virtual environment is read-only; both source roots must resolve to the
isolated worktree. No merge, deployment or runtime acceptance is implied by
local implementation or verification.
