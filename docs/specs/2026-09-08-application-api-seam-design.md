# Application API seam — a federation of subsystems behind one versioned contract

**Date:** 2026-09-08
**Status:** Design (companion to the compiler seam; no tracker yet)
**Baseline:** `release/0.8.0` @ `ad5421518`. Every count below was taken from
that tree with the instrument named beside it; none is inherited from an
earlier document.
**Context:** The 2026-09-07 ownership investigation
([docs/arch-analysis-2026-09-07-web-split/](../arch-analysis-2026-09-07-web-split/04-final-report.md),
commit `8702d7d5a`) concluded that browser UX moves to a specialist team while
the engine, the Composer, and every Python module under `src/elspeth/web/`
stay with the current team, and that "the API [must become] a supported product
interface before promising that either side can release independently." This
document is that interface's design. It is the wire-side companion to the two
compiler seam documents
([2026-04-15](2026-04-15-compiled-pipeline-architecture-design.md),
[2026-08-20](2026-08-20-compiler-facade-mvp-sketch.md)): the compiler seam
sits *inside* the backend between authoring and execution; this seam sits
*around* the backend between it and every client.

---

## The thesis

The seam the other team consumes is the HTTP wire, not any Python package
boundary. Everything the wire fronts is a **loose federation of subsystems** —
sessions, composer, execution, catalog, blobs, auth, secrets, preferences,
audit-readiness, shareable-reviews — each already mounted as its own router
with its own strict Pydantic wire models. What they lack is a shared identity:
one versioned contract they all publish into, one artifact a client can be
built against, and one gate that notices when the artifact moves.

The design therefore adds almost no new surface. It gives the federation a
version, snapshots the schema it already generates, derives the client types
from that snapshot instead of by hand, and makes the strictness that both
sides already enforce *negotiated* rather than *accidental*. Two consumers
keep the contract honest: the browser application the web team will own, and
any non-browser client (CLI, MCP host, or a future headless agent) that speaks
the same contract.

## What already exists

Measured at `ad5421518`.

| Piece | Location | Measured | Instrument |
|---|---|---|---|
| Route surface | `src/elspeth/web/**/routes*.py`, `app.py` | 106 route decorators, 1 of them WebSocket | AST walk over `@router.<verb>(` decorators |
| Typed responses | same | 94 of 105 HTTP routes declare `response_model` or a typed return | same walk |
| Untyped responses | `blobs/routes.py` (3), `app.py` (2), `secrets/routes.py`, `composer/tutorial_abandon_routes.py`, `auth/admin_routes.py`, `auth/routes.py`, `execution/routes.py`, `sessions/routes/sessions.py` (1 each) | 11 | same walk |
| Strict server wire models | `web/**` Pydantic models | 139 `extra="forbid"` vs 8 `extra="allow"/"ignore"` | grep over `model_config` |
| Strict client decoders | `frontend/src/api/guidedDecoder.ts` (`exactRecord`) | rejects unknown keys by design | source read |
| Single client entry point | `frontend/src/api/client.ts` | 70 distinct `/api` paths; **0** referenced outside `src/api/` | regex over non-test `.ts/.tsx` |
| Generated schema | FastAPI default `/openapi.json` | exposed; `version="0.1.0"` hard-coded (`app.py:1169`) while the product is 0.8.0 | source read |
| Type generation | `frontend/package.json` `generate-types` → `openapi-typescript … -o src/types/api.generated.ts` | script present; **output file does not exist**; `types/api.ts:2` says "hand-written" | `ls`, source read |
| Producer-generated fixture | `frontend/src/api/__fixtures__/gateProposalProjection.json`, `scripts/cicd/bootstrap_proposal_diff_fixture.py` | one family covered | `ls` |
| Cross-language parity tests | `tests/unit/web/composer/test_graph_topology_parity.py`, `test_semantic_edge_contract_parity.py`, `guided/test_gate_projection_fixture.py` | exist, read frontend sources directly | `ls` |
| OpenAPI already used as an oracle | `tests/unit/web/composer/guided/test_no_chain_authoring_path.py:93,163` | asserts retired contracts are absent from and required paths present in `app.openapi()` | source read |
| Served-build identity | `app.py:937 _frontend_build_identity`, `app.state.frontend_build` (`:2035`) | server already parses the SPA bundle identity it serves | source read |
| Closed progress vocabulary | `contracts/composer_progress.py` (`ComposerProgressPhase`, `ComposerProgressReason`, `_StrictProgressModel`) | mirrored by hand in TypeScript | source read |
| Streaming channels | `execution/routes.py:1531 @router.websocket("/ws/runs/{run_id}")` gated by `POST /api/runs/{id}/ws-ticket`; `GET /api/sessions/{id}/composer-progress` polled | two channels, two mechanisms | source read |
| Auth transport | `client.ts:92` | `Authorization: Bearer` from `localStorage`; global 401 interceptor | source read |
| Origin assumptions | `client.ts:4` relative URLs; `config.py:225 cors_origins`, `:230 public_base_url` | single public origin | source read |

Two of these deserve emphasis because they invert the usual API-hardening
story. **The strictness is already there on both sides.** 139 server models
forbid unknown fields and the guided decoder refuses them; the Tier-1 posture
of [ADR-032](../architecture/adr/032-validate-by-trust-domain.md) is
enforced, not aspirational. And **the consumer is already disciplined**: every
frontend call goes through one client module. What is missing is not
discipline but *identity* — the contract has no version, no snapshot, and no
generated consumer, so the strictness has nothing to negotiate against and
fails as a surprise.

## What is missing

1. **A contract version.** `version="0.1.0"` names nothing. A strict client
   and a strict server that disagree have no way to say so before the first
   decoder throws.
2. **A snapshot.** `/openapi.json` is regenerated on every boot and never
   compared. A schema change is invisible until a client breaks.
3. **A generated consumer.** The TypeScript types are hand-written mirrors;
   the generation script exists but was never adopted. Drift is detected by
   humans reading two files.
4. **Eleven untyped routes.** Their shapes exist only in handler bodies, so
   the generated document is authoritative for 94 routes and silent for 11.
5. **Streaming outside the document.** OpenAPI does not describe the
   WebSocket run channel or the polled progress channel; their schemas
   (`RunEvent`, `ComposerProgressEvent`) are strict but unpublished.
6. **Domain semantics in the browser.** `frontend/src/lib/graphTopology.ts`
   encodes fan-in and connection semantics (flagged in the 09-07 report). The
   browser is a topology authority it should not be.
7. **Fixtures for one family only.** The proposal-projection fixture is the
   right pattern applied once.

## Decisions

Settled with the operator on 2026-09-08.

### D1 — Strict on both sides, with an explicit contract version

Keep `extra="forbid"` on the server and exact-key decoding on the client.
Compatibility is achieved by **negotiation, not tolerance**: the client
declares the contract version it was built against; the server serves that
version or refuses with a typed error. Under a strict posture every schema
change is by definition a breaking change for some consumer, so the version is
a **single monotonic integer** bumped on *any* schema diff. There is no
additive-is-free rule to litigate.

Rejected: tolerant decoders (relaxes a posture 139 models enforce and turns
the client into the fail-open guard [ADR-032] warns against); lockstep-only
(forecloses the independence the handoff exists to enable).

### D2 — One contract, per-subsystem fragments

The federation publishes **one** OpenAPI document with **one** version. Inside
it each subsystem owns a fragment: its routes (already distinguished by router
prefix and tag), its wire models (already one `schemas.py`/`models.py` per
subsystem), and its fixtures. Ownership is legible without multiplying
handshakes, and the types several fragments share (`ValidationResult` appears
in both sessions and execution) live once.

Rejected: per-subsystem versions (N negotiations, awkward shared types);
one monolith without fragments (loses the ownership structure the split
requires).

### D3 — The snapshot is the artifact; the diff is the gate

`config/cicd/api_contract/openapi.v<N>.json` is a tracked, canonicalised
(`sort_keys`, compact separators, as `test_no_chain_authoring_path` already
does) snapshot of `app.openapi()`. A unit test regenerates the document from
the live app and asserts byte equality. Changing a wire model without
re-snapshotting reds the tree; re-snapshotting without bumping `<N>` reds the
tree. This is the same shape as the existing wire-shape and declared-oracle
gates in [CONTRIBUTING § Whole-tree gates](../../CONTRIBUTING.md#whole-tree-gates-and-conventions-you-will-hit):
pin the bytes, fail on drift.

### D4 — Client types are generated, never hand-written

`src/types/api.generated.ts` is produced from the snapshot (not from a running
server) by the existing `openapi-typescript` script and committed. A frontend
CI check regenerates and diffs it. `types/api.ts` becomes the re-export shim
its own header already anticipates; the hand-written mirrors in `types/*.ts`
are deleted as each fragment is covered. Runtime decoders remain (generated
types are compile-time only) but are derived from the generated types so a
decoder cannot accept a shape the contract does not name.

### D5 — Streaming channels are part of the contract

`RunEvent` (WebSocket) and `ComposerProgressEvent` (polled) are published as
named components in the same document, with a prose section per channel
covering ticket acquisition, reconnect, snapshot-on-reconnect, terminal
events, and the polling contract. OpenAPI cannot describe a WebSocket's
lifecycle; the document describes its *messages* and the prose describes the
*protocol*. An AsyncAPI companion is deferred until a second streaming
consumer exists.

### D6 — Topology is resolved server-side

The API ships resolved graph topology (connections, fan-in, orphan status)
computed by the backend's `_producer_resolver` and related composer modules.
The browser renders; it does not decide. The existing parity tests that read
frontend sources are kept until the fixture-based conformance check for the
topology fragment replaces them, then retired.

### D7 — Composer invariants are unchanged by the seam

The provider authors pipeline structure and the tutorial uses the same backend
path as every session ([AGENTS.md § Composer invariants](../../AGENTS.md)).
Nothing in this seam moves authoring, validation, or admission into a client.
A client that needs a pipeline shaped differently asks the composer; it does
not build one.

### D8 — Any client is a first-class consumer

The contract is designed for the browser *and* for non-browser clients. Today
`composer_mcp/` and the CLI reach the composer in-process, not over the wire;
they are not consumers of this contract and nothing here changes that. But the
contract must not assume a browser: no route may depend on `localStorage`
semantics, same-origin cookies, or a rendered `index.html` to be usable. A
headless client built against the snapshot is the second consumer that keeps
the first honest.

## The federation

Members, measured at `ad5421518` by the same AST walk. Route counts are per
source directory or file; the tutorial and dev-admin routers are listed so the
total reconciles to 106.

| Member | Router prefix | Routes | Wire models | Fragment owner |
|---|---|---|---|---|
| sessions (incl. composer, guided, interpretations, proposals, state, runs-by-session) | `/api/sessions` | 38 | `sessions/schemas.py`, `sessions/audit_story_models.py`, `composer/guided/protocol.py`, `contracts/composer_progress.py` | backend |
| execution | `/api/runs`, `/ws/runs/{run_id}` | 12 | `execution/schemas.py` | backend |
| auth | `/api/auth` | 10 | `auth/*` (user profile, token, SSO handoff) | backend |
| blobs | `/api/sessions/{id}/blobs` | 7 | `blobs/schemas.py` | backend |
| catalog | `/api/catalog` | 5 | `catalog/schemas.py`, `catalog/knob_schema.py` | backend |
| secrets | `/api/secrets` | 4 | `secrets/schemas.py` | backend |
| shareable-reviews | `/api/sessions/{id}/mark-ready-for-review`, `/api/sessions/{id}/shareable-link`, `/api/sessions/shared/{token}` | 3 | `shareable_reviews/models.py` | backend |
| preferences | `/api/composer-preferences` | 2 | `preferences/models.py` | backend |
| audit-readiness | `/api/sessions/{id}/audit-readiness` | 2 | `audit_readiness/models.py` | backend |
| tutorial | `/api/tutorial` | in the remainder | `composer/tutorial_models.py` | backend |
| dev-admin, identity-admin | `/api/auth/admin/*` | in the remainder | `auth/admin_routes.py`, `auth/identity_admin_routes.py` | backend |
| platform (`app.py`) | `/api/health`, `/api/ready`, `/api/system/status`, `/metrics` (already `include_in_schema=False`, `app.py:2092`) | 4 | inline in `app.py` | backend |
| **remainder** (tutorial + admin + `app.py`) | | 23 | | |
| **total** | | **106** | | |

Every fragment owner is the backend team. The web team owns the *client* of
each fragment. The 09-07 deconfliction list already names the twelve schema
modules as "backend remains contract authority"; this table is the same
allocation expressed as fragments.

A fragment is complete when it has: every route typed (`response_model` or
typed return), its models inheriting a strict base, at least one
producer-generated fixture per response family, and its prose semantics (error
categories, idempotency, retry, operation identity) in the contract document.

## Version negotiation

- **Contract version** is an integer `N`, starting at `1`, carried in the
  snapshot filename and in the OpenAPI `info.version`. The FastAPI `version`
  string is set from it, replacing `"0.1.0"`. It is independent of the package
  version; the two move for different reasons.
- **Client declaration.** Every request carries `X-Elspeth-Api-Version: N`.
  The generated client sets it from a constant emitted alongside
  `api.generated.ts`. A missing header is treated as "unknown client" and
  refused in production, accepted in development (mirrors how `cors_origins`
  defaults are already development-shaped).
- **Server response.** `/api/system/status` (exists today) advertises
  `api_versions_supported: [N]` and the served `frontend_build` (already
  computed at `app.py:2035`). A mismatch returns HTTP 409 with a typed body
  `{"code": "api_version_unsupported", "requested": n, "supported": [N]}`.
  The client's existing global interceptor (`client.ts:5`) gains an arm for it
  and surfaces "this UI was built against an older API" instead of a decoder
  stack trace.
- **MVP supports one version.** The server serves exactly the current `N`.
  The negotiation exists so a mismatch is *loud and attributable* on day one;
  serving two versions concurrently is the release-independence decision the
  09-07 report defers to evidence ("Decide release independence from
  evidence") and is out of scope here. Adding it later changes the supported
  set, not the mechanism.
- **Why an integer, not semver.** Under D1 there are no non-breaking schema
  changes, so minor and patch have no meaning on the wire. Prose-only edits to
  the contract document do not bump `N`; schema bytes do.

## Contract artifact and gates

```
config/cicd/api_contract/
  openapi.v1.json            # canonicalised app.openapi(); byte-pinned
  fixtures/
    sessions/…json           # producer-generated, one per response family
    execution/…json
    …
src/elspeth/web/frontend/src/types/
  api.generated.ts           # from openapi.v1.json via openapi-typescript
  api.ts                     # re-export shim (already framed for this)
```

Gates, in the order they are added:

1. `test_api_contract_snapshot_matches_live_app` — regenerate, canonicalise,
   compare bytes. Reds on any schema drift. Sibling of the existing
   `test_no_chain_authoring_path` oracle.
2. `test_api_contract_version_bumped_with_snapshot` — if the snapshot changed
   relative to the previous tracked file, `N` must have changed.
3. Frontend CI: regenerate `api.generated.ts` from the snapshot and diff.
4. `test_every_route_declares_a_response_model` — AST or `app.routes`
   walk; the 11 untyped routes are the initial red and the fix list.
5. Fixture round-trips per fragment: the producer builds the model, dumps the
   fixture, and a frontend test decodes it with the generated types. Replaces
   the three parity tests that read frontend sources once every fragment they
   cover has a fixture.

The snapshot is **not** signed. It protects a development seam between two
teams, not a release artifact or runtime data; per
[ADR-046](../architecture/adr/046-audit-grade-is-a-product-characteristic.md)
that is ordinary hygiene, and a byte-pinned tracked file with a CI diff is
the whole safeguard. If the artifact is ever distributed outside the
repository, revisit.

## Streaming contract

**Run events.** `POST /api/runs/{id}/ws-ticket` → short-lived ticket →
`WS /ws/runs/{run_id}?ticket=…` → a stream of `RunEvent` (strict; `timestamp`
tolerates ISO strings for the reconnect round-trip and rejects epoch integers,
as the model already documents). The prose section specifies: ticket lifetime
and single-use; snapshot-on-reconnect semantics; the terminal event set
(`CompletedData`, `CancelledData`, `FailedData`); and that the WebSocket is
the only push channel — everything else is request/response.

**Composer progress.** `GET /api/sessions/{id}/composer-progress` returns the
latest `ComposerProgressEvent` (closed `phase`/`reason` vocabularies in
`contracts/composer_progress.py`). The prose specifies polling cadence
bounds, the retirement rule for a terminal snapshot (the latched "working on"
box, `elspeth-efc92391bb`, is a consumer defect this contract makes
specifiable), and that progress is advisory: the durable outcome is the
compose response, never the progress poll.

Both message types are published as components in the same document. The
protocol prose lives in the contract document beside them.

## Hosting

Unchanged for this seam: one public origin, SPA `dist` served by the same
process, `Authorization: Bearer` from browser storage, `cors_origins` for the
Vite dev proxy. The contract must remain usable without any of that (D8), but
this document does not choose separate-origin hosting. That choice needs
authentication, browser policy, and WebSocket behaviour validated for the
selected topology and belongs to the release-independence decision.

## Stages

Each stage leaves the tree green and is independently useful.

| Stage | Content | Exit |
|---|---|---|
| 0 — Measure | This document. | Counts above reproduced by anyone from the named instruments. |
| 1 — Make the schema authoritative | Type the 11 untyped routes; set `info.version` from `N=1`; add the snapshot and its two gates. | `openapi.v1.json` tracked; drift reds CI. |
| 2 — Generate the consumer | Run `generate-types` from the snapshot; commit `api.generated.ts`; convert `types/api.ts` to the shim; delete hand-written mirrors fragment by fragment; frontend diff gate. | No hand-written wire type remains for a covered fragment. |
| 3 — Negotiate | `X-Elspeth-Api-Version`; `/api/system/status` advertises supported versions and served build; 409 arm in the client interceptor. | A UI built against `N-1` fails with a named error, not a decoder throw. |
| 4 — Fixtures and streaming | One producer-generated fixture per response family; `RunEvent` and `ComposerProgressEvent` as components with protocol prose; retire the source-reading parity tests as fixtures cover them. | Web team can build and test a fragment's client offline. |
| 5 — Topology projection | Ship resolved topology from the backend; `graphTopology.ts` becomes a renderer. | Browser makes no validity or connectivity decision. |
| 6 — Independence (deferred) | Multi-version serving, separate artifacts, origin policy. | Only with evidence from stages 1–5 in use by the web team. |

Stages 1–3 are the handoff prerequisite the 09-07 report calls "make the
existing boundary consumable". Stage 4 is what lets the web team work without
a running backend. Stage 5 removes the last domain authority from the browser.
Stage 6 is explicitly not promised.

## What the contract can and cannot claim

It **can** claim: that a client built against `openapi.v<N>.json` will
decode every response a server advertising `N` returns for a documented
route; that a schema change is visible as a diff before it ships; that an
incompatible pairing is refused with a named error; and which team owns each
fragment.

It **cannot** claim: that the browser and backend can be released
independently (stage 6); that behaviour not expressed in schema — ordering,
timing, idempotency under retry — is verified by the gates (it is documented
prose, checked by the fixture round-trips only where a fixture encodes it);
that any non-browser client exists (D8 designs for one; none is built here);
or anything about authorization policy, which remains the backend's and is
never inferred from what a client can see.

## Relationship to the compiler seam

Orthogonal, and neither waits for the other. The compiler seam
([2026-08-20](2026-08-20-compiler-facade-mvp-sketch.md)) would introduce a
`CompiledPipeline` artifact between authoring and execution *behind* this
wire; when it lands, the execution fragment gains routes and models and `N`
bumps like any other change. Keeping the compiler behind the API is what lets
the web team stay ignorant of engine internals, which the 09-07 report already
required.

## Explicitly not in scope

- A separate repository or separate deployment for the frontend.
- Serving more than one contract version concurrently.
- Cross-origin hosting and its authentication consequences.
- Any change to authorization, session leasing, or blob custody semantics.
- The compiler facade, or reconciling the two compiler proposals.
- The internal Python seams inside `web/composer/` (tool registry init,
  `_common.py` hub, `protocol.py` → `execution.schemas` edge, blob-plane
  concrete imports, `tool_batch` → guided). Those are backend hygiene on the
  retained side of the line and are tracked separately; they do not gate the
  handoff.
- Any headless agent or fork. D8 requires the contract to admit one; building
  one is not this project's work.

## Open questions

1. **Header versus path versioning.** This design uses a request header so
   routes keep their current paths and the frontend's 70 references do not
   move. A `/api/v1/` prefix is more conventional and more visible in logs.
   Decide before stage 3; the snapshot gate (stage 1) is unaffected either way.
2. **Where the protocol prose lives.** Options: a tracked Markdown beside the
   snapshot under `config/cicd/api_contract/`, or `docs/reference/api.md` with
   the snapshot as its generated appendix. The reference directory already
   holds `composer-tools.md`, which argues for the latter.
3. **Fixture generation cadence.** Regenerate on every model change (simple,
   noisy diffs) or only when a fragment's version-relevant bytes change
   (quieter, needs the snapshot diff to drive it). Stage 4 decision.
4. **Dev-admin routes in the contract.** `/api/auth/admin/users` is a
   development convenience. Publishing it in the same document makes it a
   promise. The route-level opt-out already exists — `/metrics` is mounted
   with `include_in_schema=False` (`app.py:2092`) and so never reaches the
   snapshot — so the decision is only *which* routes use it. Lean: exclude
   the dev-admin router the same way, and have the snapshot test assert the
   excluded set explicitly so an accidental exclusion is also a diff.
