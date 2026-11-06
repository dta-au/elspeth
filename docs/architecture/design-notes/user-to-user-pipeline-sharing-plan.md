# User-to-user pipeline sharing: implementation plan

Date: 2026-11-05. Status: proposed implementation; no feature code changed.
Release assignment: not yet assigned. Source inspection baseline:
`a18168e48f24e113290e1b323b724a4bc2088aca` on
`release/0.8.10-assembly`. Reconcile with the implementation branch before
editing; this plan is not a claim that sharing belongs in 0.8.10.

## Outcome and agreed security boundary

An authenticated pipeline owner can send a pipeline to another existing user
on the same deployment. The recipient accepts it and receives an independent,
editable pipeline in their own account, with its graph reconstructed and its
required managed files copied into recipient-owned storage. Subsequent edits
and deletion of either pipeline do not affect the other.

The sender decides whether the recipient has a need to know. The product does
not add a classification system, compartment access checks, approval workflow,
or administrator sign-off to that decision. It still enforces identity,
ownership, recipient binding, file custody, quotas, and the prohibition on
transferring credentials. Existing execution policy continues to apply.

This is a copy operation, not collaborative editing or a subscription to future
changes. Sending a newer revision creates a new offer. The initial scope is
same-deployment accounts, one named recipient per offer, and explicit acceptance.
There is no email delivery, public copy link, cross-deployment package import,
or automatic execution in this increment.

## Existing implementation and the actual gap

The following are source observations, not conclusions from status reports.
Paths and symbols refer to the baseline above.

| Existing mechanism | Source evidence | Reuse and boundary |
| --- | --- | --- |
| Frozen review sharing | [`ShareableReviewService`](../../../src/elspeth/web/shareable_reviews/service.py), `mark_ready_for_review`, `resolve_token`; [`review guide`](../../guides/sharing-pipelines.md) | Authenticated bearer access to a public frozen snapshot. It does not create a recipient-owned session or copy files. Keep that contract unchanged. |
| Public graph/YAML projection | [`generate_public_composition_dict`, `_recursive_public_option_projection`](../../../src/elspeth/web/composer/yaml_generator.py) | Removes storage paths and blob custody, but preserves secret-reference and environment-reference markers. It is neither a complete reconstruction bundle nor a sufficient credential transfer policy. |
| Library import/fork | [`library routes`](../../../src/elspeth/web/sessions/routes/workflow/library.py), [`Library authority`](../../../src/elspeth/web/coordination/library_authority.py), [`Sessions service`](../../../src/elspeth/web/sessions/service.py) | Creates a session for the importing user from YAML. Library publication refuses uploaded blob-backed sources; import supplies no source blob bindings. Use recipient-session creation conventions, not Library as an implicit file-transfer mechanism. |
| Same-owner session fork | [`SessionService.fork_session`](../../../src/elspeth/web/sessions/service.py), [`fork route and state rewrite`](../../../src/elspeth/web/sessions/routes/sessions.py), [`fork custody settlement`](../../../src/elspeth/web/sessions/fork_custody.py) | Stages a hidden child, freezes a blob plan, rewrites custody and activates after settlement. Also inherits owner/auth context and conversation history. Do not expose it as cross-user sharing. |
| Durable file copy | [`BlobService.copy_blobs_for_fork`](../../../src/elspeth/web/blobs/service.py), [`blob contracts`](../../../src/elspeth/contracts/blobs.py) | Useful integrity, quota, retry and cleanup patterns. Its same-principal checks are intentional and must remain. Add a separate typed sharing authority for cross-principal transfer. |
| Credential resolution | [`WebSecretService.resolve`](../../../src/elspeth/web/secrets/service.py), [`secret wiring policy`](../../../src/elspeth/web/secrets/wiring_policy.py) | Unscoped resolution can fall back from user to server secrets. Copying an alias can silently select a different credential. Remove bindings and require explicit recipient remapping. |
| Credential detection | [`credential_material.py`](../../../src/elspeth/contracts/credential_material.py) | Existing finite, versioned detector recognizes credential fields, token shapes, private keys and credential-bearing URLs. Reuse its authority; do not invent a competing regex policy. |
| Recipient directory | [`people_routes.py`](../../../src/elspeth/web/auth/people_routes.py) | General People access is capability gated. Add a narrow ordinary-user recipient lookup rather than relaxing the admin directory. |

Meaningful existing test bodies were also inspected: the cross-user Library
workflow in [`test_library_workflow.py`](../../../tests/integration/web/test_library_workflow.py),
cross-principal refusal and idempotent blob copying in
[`blobs/test_service.py`](../../../tests/unit/web/blobs/test_service.py),
production-chain settlement in
[`test_fork_custody_settlement.py`](../../../tests/unit/web/sessions/test_fork_custody_settlement.py),
quota growth during copying in
[`test_storage_quota_sites.py`](../../../tests/unit/web/blobs/test_storage_quota_sites.py),
commit-ack loss in
[`test_fork_deletion_recovery_ack.py`](../../../tests/unit/web/blobs/test_fork_deletion_recovery_ack.py),
and PostgreSQL lock behavior in
[`test_blob_custody_lock_isolation_postgres.py`](../../../tests/testcontainer/web/test_blob_custody_lock_isolation_postgres.py).
These support reuse decisions; they were not executed for this planning task
and do not establish that cross-user file sharing already works.

## What is transferred

| Item | Required behavior |
| --- | --- |
| Authored graph | Preserve sources, nodes, edges, routing, plugin configuration, prompts and templates that pass the transfer projection. Preserve graph-local identifiers where they carry semantics; allocate new session and state identifiers. Copy the authored structure without calling the planner to invent a replacement. |
| Graph-owned files | Transfer the transitive set of managed files required by the selected graph revision, including supported source bindings and referenced inline-content blobs. Deduplicate repeated references within the offer. New blob records and recipient custody; accepted bytes and content hashes remain identical. |
| Unreferenced uploads | Excluded. Show the included file list before sending; do not silently copy every session blob or attachment. |
| Outputs and runtime stores | Do not transfer prior outputs, Landscape stores, checkpoints, caches or run artifacts. Allocate fresh recipient output locations. Refuse an unsupported persistence dependency rather than leaving a sender path in the copy. |
| Remote resources | Preserve only supported non-secret configuration. Do not fetch URLs or read arbitrary host paths while sharing. Recipient configuration remains necessary for external connections; remote content is not represented as a copied file. |
| Credentials | No secret values, encrypted secret rows, secret fingerprints, owner secret aliases, secret scopes, environment bindings, signed URLs or authenticated connection strings in the transfer payload. Replace legitimate credential bindings with unresolved requirements. Reject literal credential material in other graph content. |
| Session context | No conversation, tool responses, private diagnostics, provider context, auth/session tokens, approval facts, execution history or inherited readiness verdicts. Start a fresh recipient session and recompute derived metadata. |
| Provenance | Record sender, recipient, source revision and transfer operation internally. Expose only the minimal sender label and copy provenance needed by the recipient. Provenance never authorizes reads of the sender's session. |

Create an explicit transfer projection from the owned graph model, not a raw
database row, `to_dict()` dump, or redacted review payload. Use a positive list
for transferable authoring metadata. Unknown custody-bearing fields or unsupported
file carriers must produce a named refusal before an offer is published.

In particular, a local retrieval index at
`options.provider_config.persist_directory` is a directory dependency, not an
uploaded blob. Refuse it in v1 unless an explicit snapshot adapter is implemented;
the existing fork rewriter does not make that directory portable. Operator
profiles require a separate boundary: capture authored safe configuration, never
the lowered executable configuration that injects server credentials. Any retained
profile alias requires recipient confirmation and validation against current
deployment policy; it must not silently authorize a connection in the copy.

### Secrets and file contents

Separate an unresolved credential requirement from executable plugin options.
Persist a typed requirement containing the destination component, option path,
and expected credential kind, without the original value, alias, scope or
fingerprint. Remove the original binding from the transferred options. A new
session may open as an incomplete draft; execution and credential-dependent
preview paths remain blocked until the recipient maps each requirement through
the ordinary secret selection and wiring policy. Never insert a dummy string
that a plugin could treat as a real credential. An identically named recipient
or server secret must not be selected implicitly.

This requires a deliberate draft-import/readiness contract. The existing YAML
seeder and plugin validators can reject a required credential field that is now
absent. Implement graph-native staged hydration plus the typed requirement sidecar
and narrowly defined draft validation; do not weaken ordinary execution validation
or assume the current importer already accepts this state. Requirement paths must
be structured component/field segments so dots and indexes are unambiguous.
Classify validation failures explicitly: only missing credential/profile bindings
represented by validated requirements permit activation as an incomplete draft.
Structural, plugin-policy, file-custody and unrelated validation failures still
refuse activation. Do not reuse a blanket `persist_invalid` escape. Persist the
requirements across reload, graph edits and session copies; removing or changing
a referenced component must resolve or invalidate the corresponding requirement
through the same authorized state mutation, not leave a bypassable UI warning.

Perform credential checks before persisting the share payload and again against
the exact reconstructed candidate before activation. Sharing does not decrypt,
enumerate values from, or copy the sender's secret store. Scan graph strings,
names, descriptions, filenames and supported file contents using the existing
credential detector. A rejection reports a safe component/file location and
category, never the matched value or surrounding text. Apply the same rule to
errors, logs, operation receipts and preview responses.

For v1, support the existing managed text-data families: CSV, JSON, JSONL and
plain text, with explicit supported encoding handling. The upload MIME sniffer
in [`blobs/sniff.py`](../../../src/elspeth/web/blobs/sniff.py) inspects a prefix;
that is not a credential scan. Inspect the entire admitted file, decode strictly,
and inspect structured fields where appropriate (for example CSV header/value
pairs). Use bounded parsers and total byte/record/depth/time limits. A decoding,
parsing, resource-limit or unsupported-format failure refuses transfer; it is
never a clean scan. Do not use lossy decoding or sample only the beginning.
Large inputs require a reviewed streaming strategy that cannot miss credentials
at chunk boundaries; until then, refuse files beyond the explicit scan limit.

Known credentials block sharing. Do not redact data files in place or copy a
partial file. The sender must remove the credential from the source and create
a new offer. Accepted files retain exact bytes; bind scan completion to their
hash, size and detector version, and verify those bytes during copy.

The detector has an explicit limitation: it cannot establish that arbitrary
encoded, encrypted, split or disguised data contains no secret. The enforceable
promise is that managed secrets and their access bindings never transfer, and
recognized credential material is refused. This is not a general DLP guarantee.
Unsupported opaque formats are refused in v1. Need-to-know delegation does not
provide a bypass for a credential refusal.

## Proposed user flow and API

1. Owner chooses **Send a copy** from the current pipeline. The separate
   **Share for review** action keeps its existing read-only behavior.
2. Owner selects an existing recipient through a minimal account lookup. Bind
   the result to the canonical identity and provider context, never just a
   display name or a client-supplied owner field. No broad directory or secret
   inventory is exposed.
3. A preview shows the exact graph revision, included files and total bytes,
   credential requirements that will need reconnecting, and unsupported
   dependencies. The owner confirms disclosure to that recipient. The request
   carries the preview revision; a concurrent edit requires a refreshed preview.
4. The recipient sees the offer in **Shared with me** or follows its authenticated
   recipient-bound link. They can inspect the sanitized graph and file inventory,
   accept, or decline. The sender can revoke a pending offer.
5. Acceptance shows copy progress. The recipient's ordinary pipeline list shows
   the new session only after complete reconstruction. The resulting draft
   identifies any credentials or external settings still needing configuration.
6. Recipient explicitly selects their own permitted secrets, validates the
   reconstructed pipeline, and runs it through existing execution controls.

Proposed routes, to be reconciled with existing router conventions:

| Route | Authority and result |
| --- | --- |
| `GET /api/pipeline-share-recipients?query=…` | Authenticated exact/bounded lookup; minimal stable recipient identity and label; rate limited. No account administration fields. |
| `POST /api/sessions/{session_id}/pipeline-shares/preview` | Owner only; version-bound sanitized preview, dependency inventory and refusals. No persisted offer yet. |
| `POST /api/sessions/{session_id}/pipeline-shares` | Owner only; recipient and source revision; idempotently publish a validated offer. |
| `GET /api/pipeline-shares?direction=received\|sent` | Current principal's offers only; bounded pagination. |
| `GET /api/pipeline-shares/{share_id}` | Named sender or recipient only; sanitized preview and operation status. An identifier is not a bearer capability. |
| `POST /api/pipeline-shares/{share_id}/accept` | Exact recipient only; idempotent asynchronous reconstruction, then the same destination session ID on replay. |
| `POST /api/pipeline-shares/{share_id}/decline` | Recipient only; pending offer becomes terminal. |
| `POST /api/pipeline-shares/{share_id}/revoke` | Sender only; prevents uncommitted acceptance. Does not delete an accepted recipient copy. |

Use existing authentication, CSRF, rate-limit and error conventions. Unknown or
unauthorized share IDs must not disclose another user's metadata. Do not upgrade
existing shared-review tokens into copy permissions. No notification service is
required: an in-app inbox and copyable recipient-bound link are sufficient.
Require the live human pipeline `user` role through the existing middleware at
offer creation and acceptance; an authenticated active identity alone is not
sufficient. Recheck recipient role/status transactionally at activation, so a
role revoked during copying cannot leave a newly activated pipeline.

## Persistence, copy lifecycle and recovery

Add a dedicated `pipeline_shares` feature and typed coordination authority.
Reuse lower-level integrity/custody primitives where their contracts fit; do not
add an `allow_cross_user` boolean to the same-owner fork interface.

An offer stores immutable sender/recipient principals, source revision,
sanitized graph payload, credential requirements, internal file manifest,
expiry, lifecycle state and destination binding. The file manifest records
source custody, hash, size and the approved target mapping. Source paths and
identifiers needed internally are not returned in recipient previews. Allocate
fresh destination IDs and retain the mapping for retries. A relational unique
constraint enforces one destination per offer; API idempotency keys bind to the
request identity and payload, rejecting conflicting reuse.

The offer freezes graph content and pins the referenced immutable file versions.
Later graph edits do not change it. Pending offers have a finite configurable
lifetime. Retain source bytes under explicit share custody until acceptance,
revocation, decline or expiry has settled; never rely on an incidental filesystem
path surviving. This avoids a second full file copy merely to create an offer.
Pending references must participate in blob replacement, deletion, session purge
and garbage collection. For v1, destructive deletion of a pinned source reports
the outstanding offers and requires revocation/expiry plus copy settlement first.
Editing may bind a new blob while the pinned older version remains retained.

Acceptance follows this sequence:

1. Authenticate the recipient and recheck active account, offer state, expiry,
   source custody, file integrity and recipient storage/session quotas. Reserve
   capacity atomically so concurrent accepts cannot both spend the same budget.
2. Under the existing fencing/lease discipline, claim the offer and create one
   hidden recipient-owned destination session and durable copy operation. Bind
   recipient auth context and current applicable policy, not the sender's.
3. Copy only manifest files through the blob service with a sharing-specific
   authority. Verify hash/size, enforce bounded I/O concurrency, and write
   idempotently to recipient storage. Never grant the recipient direct access to
   sender blob endpoints, use hardlinks to mutable files, or traverse raw paths.
4. Reconstruct the graph using the exact file mapping. Rewrite every supported
   source/blob/template carrier and allocate fresh output paths. Assert no sender
   storage path or blob reference remains. Recompute derived metadata and fresh
   validation; incomplete credential requirements are allowed as draft state,
   malformed structure or unresolved file custody is not.
5. Recheck the live fence, recipient eligibility, expiry and revocation in the
   activation transaction. Atomically publish the complete session, mark the
   offer accepted, bind the destination ID and settle quota accounting. This is
   the linearization point for accept versus revoke/expire. Whichever commits
   first determines the result; a stale copier cannot activate afterward.
6. Release source pins after durable settlement. A repeated accepted request
   returns the original destination; it never creates another session.

Partial files and hidden sessions are inaccessible through list, direct GET,
blob, websocket, export and run surfaces. Recovery after process death resumes
the same fenced operation or cleans only its own authorized partial copies.
Do not claim a single SQL transaction makes filesystem writes atomic. Retain
durable cleanup obligations until acknowledged, and release quota reservations
exactly once. Quota/preflight refusals leave a pending offer retryable. A failed
copy may retry only after its prior fence and cleanup state are reconciled;
integrity failure is terminal and cannot silently select newer source bytes.

Revocation, expiry and decline prevent activation and release pins only after
active readers/copies settle. They do not retract an already accepted copy.
Deletion of the original after acceptance must not affect recipient files.
Account disable/deletion must prevent pending acceptance; integrate with existing
identity lifecycle and deletion policy rather than introducing a new account
retention policy. Audit create, accept, decline, revoke, expiry and failure using
value-free records. Acceptance provenance is not a copied approval or run audit.

## Ordered implementation units

Each unit includes its own meaningful tests; do not defer authorization or
failure-path coverage to the last unit. Proposed new files below do not exist yet.

### 1. Transfer contract and secret-free projection

Create `src/elspeth/web/pipeline_shares/models.py`, `projection.py` and
`dependencies.py`. Define owned immutable transfer types, credential requirement
locations, manifest entries and safe failure categories. Inspect each supported
file carrier against the plugin/config registry and real graph shapes; do not
declare completeness from a regex or source inventory alone. Adapt the current
fork rewriter only where its semantics apply. Include nested inline-content
references and shared references to the same blob. Refuse unsupported carriers.

Use `CompositionState`, `contracts/credential_material.py`, `core/secrets.py`
and `web/secrets/ref_policy.py` as existing authorities. Add bounded file scanning
in a separate module with exact byte/hash binding. No secret-store value reads.
Add `tests/unit/web/pipeline_shares/test_projection.py`,
`test_dependencies.py` and `test_file_credentials.py` with ordinary fixtures,
known credential positives, non-secret negatives, nested references, encoding
boundaries, full-file scanning and limit refusal. Changing a credential to a
recognized test token must turn an accepted case into refusal.

### 2. Offer persistence and recipient identity

Extend `web/sessions/schema.py` and owned repository/contracts with offers,
file pins, idempotency, destination binding and cleanup obligations. Add
`web/coordination/pipeline_share_authority.py`; route all writes through named
typed authority and update the Sessions writer manifest honestly. Add a narrow
recipient lookup using existing identity repository and auth conventions.

Test principal/provider collisions, ordinary-user lookup, disabled accounts,
spoofed recipients, third-party access, conflicting idempotency keys, immutable
revision binding, expiry and source-delete protection. Add PostgreSQL tests for
unique destination and pin/quota concurrency; SQLite alone is insufficient.

### 3. Recipient-owned reconstruction and settlement

Add `web/pipeline_shares/service.py` and `reconstruction.py`, plus explicit
sharing operations in `web/blobs/protocol.py` / `contracts/blobs.py` and
`web/blobs/service.py` as appropriate to the actual protocol ownership. Extend
session creation/activation and cleanup through existing authority seams.
Keep same-owner fork behavior and checks unchanged.

Implement fenced acceptance, quota reservation, frozen file copying, reference
rewriting, fresh draft creation, atomic visibility and durable cleanup. Integrate
source pins with blob deletion/replacement, session purge and identity lifecycle.
Use a bounded copy worker within current host capacity policy, not one unbounded
worker per incoming offer.

Extend the existing fork/blob custody regression suites and add sharing-specific
tests for byte equality, complete remapping, source deletion after acceptance,
recipient-only access, no copied chat/auth state, restart after each durable
boundary, fence loss, concurrent accept/revoke/expiry/delete, quota contention,
and cleanup replay. Exercise PostgreSQL with two independent service instances.

### 4. API and operational lifecycle

Add `web/pipeline_shares/routes.py` and `schemas.py`, register them through the
existing app wiring, and expose the proposed preview/create/inbox/accept/status
operations. Add bounded expiry/recovery to the existing service maintenance
lifecycle. Configure offer lifetime, scan/file limits, pending-offer limits and
copy concurrency using existing validated settings conventions. Bind preview
revision to creation and guard against changed files between scan and copy.

Add `tests/integration/web/test_pipeline_sharing.py`: sender-to-recipient happy
path plus denied third principal, no sender blob access, stale preview,
revocation race, direct access to staged sessions, idempotent retry and safe
error/log payloads. Prove existing review links remain read-only.

### 5. Composer UI and explicit credential remapping

Add frontend API/types for pipeline shares, a **Send a copy** dialog, a
**Shared with me** view and acceptance progress. Reuse ordinary graph inspection
components without borrowing review-token authority. Add a distinct route from
`/#/shared/{token}` so old review URLs retain their meaning. Use a minimal
recipient selector, file summary and clear copy/revoke semantics.

Integrate unresolved requirements with existing secret selection and readiness
UI. Recipient mapping uses their current user-scoped secret inventory and normal
destination wiring authorization. A server-scoped secret, if ordinarily allowed,
requires an explicit recipient choice; there is no name-based fallback from the
sender's graph. Any later graph edits must keep requirement locations consistent
and execution blocked until requirements are legitimately resolved or removed.
Add server-side direct run/preview/export tests with the UI bypassed: missing or
tampered requirement metadata, rename/delete/revert/duplicate/re-share of an
incomplete draft, plugin defaults appearing later, and profile-backed credentials
must not bypass explicit binding. Include role revocation during copying even
when the identity remains active.
Preserve normal provider-backed authoring for subsequent graph changes; copy
hydration restores an existing authored graph and does not introduce a server
planner, tutorial shortcut or server-authored proposal. Ensure the new hash route
preserves login return state and cannot fall into private-session auto-resume.
Include keyboard focus restoration and accessible progress/error announcements.

Add component/API tests and a browser scenario with two separate authenticated
accounts: send, accept, see reconstructed graph/files, see blocked readiness,
select recipient secret and validate. Assert the chosen recipient credential is
used by a controlled execution adapter and the sender credential never appears
in request payloads, downloads, logs or recipient state. Exercise a third account
against the same offer URL and test stale/expired links and reload during copy.

### 6. Integration, regression and user documentation

Update the sharing guide to explain both read-only review and independent copy,
file support, credential detector limits, recipient setup, expiry and revocation.
Document operational limits and failed-copy recovery. Update release notes only
after the maintainer assigns a target release.

Run affected unit/integration suites and the whole-tree authority/schema/contract
gates affected by the implementation. Check keyless trust-tier finding sets
against the actual base; signing remains suspended. Run the full Python gate on
the integrated production tree because this changes shared persistence, custody
and coordination. Also run `pytest tests/ -m testcontainer -n 0`; the default
selection excludes PostgreSQL. Use one capacity-budgeted broad suite, with both
source roots bound to the implementation worktree and complete exit receipts.
Run frontend type/build/component checks and the two-account browser flow.

Current store policy requires fresh compatible stores and preservation/refusal
for incompatible historical stores. Rebind schema epochs at implementation time,
update the canonical schema/epoch gates, and follow that policy. This plan does
not authorize an in-place migration, reset, data wipe or epoch restamp.

## Acceptance criteria

- A named recipient obtains a new editable graph and every admitted required
  managed file under their own account. File hashes match; source deletion after
  acceptance leaves the copy intact. No sender blob/path remains in target state.
- Secret values and access bindings are absent from transfer payloads, persisted
  offer snapshots and recipient copies. Recognized literal credentials in graph
  or supported files refuse sharing. Detection errors cannot return success.
- A same-name recipient/server secret cannot bind automatically. The recipient
  explicitly reconnects credentials and passes normal wiring/readiness checks.
- An unrelated authenticated user cannot inspect or accept the offer. The
  recipient cannot read the sender's session or blobs through the share grant.
- Duplicate acceptance, process death, quota contention, revocation and source
  deletion produce one complete destination or a recoverable hidden operation,
  never a visible partial pipeline, leaked files or double quota charge.
- Existing review sharing, Library import and same-owner fork retain their
  contracts. No need-to-know approval or compartment ACL is added.
- Browser behavior is demonstrated using separate accounts; backend wiring and
  test mocks alone are not reported as evidence of the complete user flow.

Implementation order is 1 → 2 → 3 → 4 → 5 → 6. Frontend scaffolding may proceed
after route/type contracts settle, but final integration depends on the custody
and credential invariants. This plan adds no product implementation or release
qualification by itself.
