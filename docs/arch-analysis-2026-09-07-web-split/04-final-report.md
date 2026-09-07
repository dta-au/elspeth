# Transferring web UX ownership to a specialist team

Date: 2026-09-07. Source baseline: `6623010fda6bdf395d2f5d659d0aa5feb5d382cd`, `release/0.8.0`.

Closeout HEAD: `a1451bf6363a0c748419f8ce2c9cd11ad0cbb168`. The intervening change only updates the Filigree skill; investigated product files are unchanged.

The follow-up [preliminary file ownership list](05-file-ownership.md) assigns exact tracked paths and identifies responsibilities to deconflict.

## Assessment

The proposed ownership split is feasible and has an existing HTTP boundary to build on. The objective is to transfer interface development and maintenance workload. A separate repository, separate deployment, and a new compiler are distinct decisions; none is a prerequisite for assigning frontend ownership.

Start with two teams in the existing repository. Give the web team ownership of the browser application and its tests. Keep the API and application semantics with the engine/Composer team. Make the API a supported product interface before promising that either side can release independently.

This transfers a coherent responsibility, but should not be described as halving engineering effort. The backend retains substantial application logic currently located under `src/elspeth/web/`, and supporting another team's API consumption becomes explicit backend work.

## Proposed ownership

| Responsibility | Accountable team | Boundary |
| --- | --- | --- |
| Visual design, navigation, accessibility, responsive layouts, interaction design | Web UX | Browser presentation and interaction |
| Chat and graph rendering, forms, loading/error/progress displays, browser state | Web UX | Render server state and submit user intent through supported APIs |
| Frontend build, dependencies, component tests, browser test implementation | Web UX | Own the Node application and its developer workflow |
| Composer planning, provider calls, proposals, guided state transitions | Backend | Product authoring semantics remain server-owned |
| Compiler, configuration lowering, authoritative validation, execution, plugins, Landscape audit | Backend | One authoritative implementation consumed through the API |
| Identity and authorization enforcement, persisted sessions, blob custody, coordination and recovery | Backend | Browser visibility is never the authorization authority |
| Backend service operations, provider credentials and secret custody | Backend | Web clients consume capabilities without holding backend credentials |
| API schemas, events, errors, compatibility policy and representative fixtures | Backend, with web-team input | Backend publishes the contract; both teams verify conformance |
| End-to-end product journeys and integrated release acceptance | Both, with a named release owner | Preserve authoring-to-execution and audit behavior across the seam |
| User-facing incident triage and support | Both, with named routing | Web team diagnoses browser failures; backend team owns API, provider and execution failures |

The concrete initial frontend scope is `src/elspeth/web/frontend/`. Moving the entire `src/elspeth/web/` directory to the UX team would also move backend application responsibilities, defeating the stated division. For example, `src/elspeth/web/sessions/routes/composer/compose.py:100` checks identity/ownership and acquires a session operation lease; `src/elspeth/web/blobs/routes.py:233` creates uploads within an operation fence; and `src/elspeth/web/execution/routes.py:975` gates execution on ownership and an execution lease.

## Current evidence and implications

**The application API is already broader than compilation and execution.** `src/elspeth/web/frontend/src/api/client.ts` wraps authentication (`:441`), sessions (`:546`), proposals (`:775`), guided transitions (`:978`), state (`:1107`), catalog (`:1254`) and execution (`:1323`). `src/elspeth/web/frontend/src/api/websocket.ts:82` connects execution events. An API limited to `/compile` and `/run` would leave much of the required application behavior outside the handoff contract. These are inspected client capabilities, not runtime coverage certification.

**The browser application already has a package boundary, but release packaging is combined.** The root `Dockerfile:39` installs the frontend's own lockfile, `Dockerfile:45` builds its source, and `Dockerfile:101` installs its output alongside the Python package. `.github/workflows/ci.yaml:1280` defines frontend unit/typecheck checks, while `:1315` includes both frontend and backend checks in the required release aggregate. This supports ownership separation now; independent releases require additional compatibility and packaging work.

**Wire contracts are manually synchronized.** `src/elspeth/web/frontend/src/types/api.ts:2` describes handwritten API types. `src/elspeth/web/frontend/src/types/guided.ts:4` mirrors backend schemas. These are coordination obligations between teams, rather than an independently consumable contract already in place.

**Independent release compatibility needs deliberate design.** `src/elspeth/web/frontend/src/api/guidedDecoder.ts:116` rejects unexpected keys. Consequently, adding fields to affected responses can break an older UI. Generated types alone do not solve this runtime behavior. Retain deliberate boundary validation and define which protocol versions or capability combinations are supported, with explicit checks against the intended frontend/backend combinations.

**Some frontend rendering contains domain interpretation.** `src/elspeth/web/frontend/src/lib/graphTopology.ts:17` and `:95` encode connection/fan-in semantics. The teams need a precise rendering contract: either backend-supplied resolved topology, or specified topology semantics with shared conformance fixtures. The browser should not become an independent authority on whether a pipeline is valid.

**Tests currently span source trees.** `tests/unit/web/composer/test_graph_topology_parity.py:53` and `tests/unit/web/composer/test_semantic_edge_contract_parity.py:41` reference frontend artifacts. Preserve these protections during an ownership handoff. If repositories later separate, replace cross-tree access with a published contract/fixture artifact and equivalent producer/consumer checks before removing the old tests.

**The compiler is a separate backend improvement.** The August compiler facade document is explicitly a sketch (`docs/specs/2026-08-20-compiler-facade-mvp-sketch.md:4`), proposing a `CompiledPipeline` artifact (`:72`) and a Composer path that emits it (`:185`). The current execution service instead loads settings from YAML or a resolved configuration dictionary (`src/elspeth/web/execution/service.py:2471`), assembles `PipelineConfig` (`:2580`), and invokes the orchestrator (`:2684`). Keep the compiler behind the API so its introduction does not require the web team to understand engine internals. Do not make frontend extraction depend on completing it.

The compiler proposals also require reconciliation: April targets portable sealed graph DTOs and execution without rebuilding from YAML (`docs/specs/2026-04-15-compiled-pipeline-architecture-design.md:38`, `:1984`); August rebuilds the graph between verification gates and excludes signing and cross-host portability (`docs/specs/2026-08-20-compiler-facade-mvp-sketch.md:165`, `:196`). This is backend design work distinct from web ownership.

**Hosting currently assumes a common public origin.** `src/elspeth/web/frontend/src/api/client.ts:4` describes relative URLs; `src/elspeth/web/frontend/src/api/websocket.ts:92` derives its host from the browser location; `src/elspeth/web/frontend/vite.config.ts:53` proxies service paths during development. Keeping a common public origin can preserve those assumptions even with separate asset builds. Separate origins require explicit URL configuration and validation of browser/authentication behavior.

## What the backend should promise the web team

Use the existing API as the starting point and document its behavior, rather than designing an unrelated replacement. The contract needs to cover:

- Authentication/session lifecycle and authorization failures.
- Catalog and authoring metadata, including supported node kinds and options.
- Composer requests, proposals, acceptance/rejection, and durable guided state.
- Uploads and blob references, with server-owned custody and authorization.
- Validation diagnostics with stable identities the UI can associate with fields or nodes.
- Execution submission and supported run-control operations, status, progress, results and audit views.
- Error categories, operation identity, retry/conflict behavior, and reconnect/recovery semantics where supported.

This is a proposed contract checklist, not a verified exhaustive endpoint inventory or a claim that all listed operations already exist. Confirm coverage from the registered API and actual browser journeys before committing to a handoff date.

## Practical sequence

1. **Assign ownership without moving code.** Agree the frontend path, backend responsibility table, product decision ownership, and escalation route. Give the web team a reproducible local environment and representative development data.
2. **Make the existing boundary consumable.** Export an authoritative API schema; reconcile handwritten models and runtime decoders; document the non-schema semantics; supply representative state, error and progress fixtures. Keep existing parity gates until equivalent contract tests exist.
3. **Prove the workload handoff.** Have the web team deliver a meaningful interaction or accessibility change using only the documented API and frontend files. Separately exercise one intentional API evolution through the agreed collaboration process. Record every occasion where undocumented backend knowledge is required.
4. **Decide release independence from evidence.** If needed, introduce a frontend artifact, supported API compatibility policy, and release-pair checks. Treat same-origin asset routing and cross-origin hosting as different deployment choices, with authentication, browser policy and streaming behavior validated for the selected choice.
5. **Consider a separate repository only if it helps the teams.** By then, shared fixtures, client contracts and integration checks have an explicit distribution mechanism. The repository move should follow the established interface.

## Handoff acceptance

The handoff is credible when the web team can build and test locally, complete ordinary UX changes without editing Python, and diagnose supported API errors from documentation and fixtures. Backend changes must detect contract breakage before integration. Both teams must be able to exercise the principal authoring, validation, execution and result-review journeys against a known backend version.

Preserve the Composer invariants: the provider authors pipeline structure, and the tutorial uses the same backend path as other sessions. Neither moves into browser-side shortcuts during the split.

## Limits

This is a focused source and design-document investigation, not an implementation, complete API inventory, security audit, workload estimate, or runtime certification. No test suite or deployment was executed. The code-map refresh was cancelled before completion after source review; conclusions use current files, not stale map results. The April compiler design includes historical descriptions that must be rechecked against current source before implementation planning.
