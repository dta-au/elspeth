# Preliminary: my files, their files, and deconfliction

Prepared 2026-09-07 against `release/0.8.0` at `a1451bf6363a0c748419f8ce2c9cd11ad0cbb168`.

**My team** retains the engine, proposed compiler, Composer service and application API. **Their team** takes browser UX development and maintenance. This is a proposed workload allocation, not a repository move, access-control policy, or equal division of engineering effort.

## Exact lists

Every tracked path is assigned exactly once in these three lists. Files requiring deconfliction have a separate proposed owner in the CSV and detailed list, so coordination does not remove accountability.

| Bucket | Exact paths | Meaning |
| --- | ---: | --- |
| [My files](my-files.txt) | 4,559 | Retained by your team, excluding explicitly flagged coordination files |
| [Their files](their-files.txt) | 587 | Proposed transfer to the web team, excluding explicitly flagged coordination files |
| [Needs deconfliction](deconflict-files.txt) | 156 | Specific contract, responsibility, scope or integration questions listed below |
| [Complete spreadsheet](file-ownership.csv) | 5,302 | One row per tracked path, with bucket, proposed owner, review basis, rationale and resolution |
| [Code table](file-ownership-codes.csv) | 204 | The `topic`, `basis`, `reason` and `resolution` values the spreadsheet codes, with a row count for each |

The spreadsheet's four repeated columns are stored as short codes — `T`, `B`, `R` and `X` — that join to
[file-ownership-codes.csv](file-ownership-codes.csv) on `(column, code)`. Only `path` is genuinely
per-row: the four coded columns carry 41, 4, 116 and 42 distinct values across 5,302 rows, so writing
them out in full cost 1.3 MB of repetition and tripped the repository's 1000 KB file gate. The
`source_head` column held a single value on every row and is recorded once in the code table instead
of 5,302 times. **No row, column or value was dropped**: the coded spreadsheet plus the code table
reconstruct the original 5,302 rows exactly, which was verified by round-trip before either file was
committed. Codes are assigned by descending frequency, so `B01` is the commonest basis.

The [detailed deconfliction list](06-deconfliction-list.md) gives every flagged path and what needs to be agreed. Of its 156 paths, 54 remain proposed **mine**, 58 **theirs**, 10 need responsibilities assigned within a shared file, and 34 await a public-website scope decision. These are file counts, not 156 independent blockers or a measure of workload.

## My files: retained responsibility groups

These directory rules describe the default owner; the exact lists above take precedence for flagged exceptions.

| Current paths | Retained responsibility |
| --- | --- |
| `src/elspeth/engine/` | Execution, orchestration, scheduling and recovery |
| `src/elspeth/core/` | Configuration, DAG/validation machinery, Landscape and supporting core services |
| `src/elspeth/contracts/` | Authoritative domain contracts; browser-visible vocabulary changes need coordination where flagged |
| `src/elspeth/plugins/` | Sources, transforms, sinks and provider integrations |
| `src/elspeth/telemetry/`, `src/elspeth/testing/` | Backend telemetry and shipped Python test support |
| `src/elspeth/cli.py`, `src/elspeth/cli_formatters.py`, `src/elspeth/cli_helpers.py`, `src/elspeth/cli_plugins.py`, `src/elspeth/mcp/`, `src/elspeth/composer_mcp/`, `src/elspeth/tui/` | Non-browser entry points and tools |
| `src/elspeth/web/composer/` | Planner/provider loop, tools, guided semantics, validation/proposal producers and authoring audit |
| `src/elspeth/web/sessions/`, `src/elspeth/web/coordination/`, `src/elspeth/web/auth/`, `src/elspeth/web/secrets/` | Persisted workflow state, leases, identity, authorization and secrets |
| `src/elspeth/web/execution/`, `src/elspeth/web/blobs/`, `src/elspeth/web/catalog/`, `src/elspeth/web/plugin_policy/` | Application API services and authoritative runtime/policy behavior |
| Remaining Python under `src/elspeth/web/`, including `audit_readiness/`, `shareable_reviews/`, `preferences/`, deployment acceptance and startup | Backend application and operational behavior; the word “web” does not make this UX code |
| `tests/`, `elspeth-lints/`, `config/`, `examples/`, `gateway/`, most `scripts/`, `evals/`, `deploy/` | Retained backend tests, policy tooling, runtime examples, gateway and operations; exact integration exceptions are flagged |
| `pyproject.toml`, `uv.lock`, backend architecture/specification/reference documents | Python packaging and backend technical documentation |

Both compiler proposals remain yours. They are listed under deconfliction because their intended artifacts and execution models differ; resolving that is your team's design decision and does not block the UX handoff. No proposed future compiler files have been invented in this inventory.

## Their files: proposed transfer groups

| Current paths | Proposed web-team responsibility |
| --- | --- |
| `src/elspeth/web/frontend/src/components/` | Screens, visual components, forms, chat, graph/review rendering and accessibility |
| `src/elspeth/web/frontend/src/styles/`, `src/elspeth/web/frontend/public/`, frontend entry points | Browser styling, static app assets and app composition |
| Frontend `src/hooks/`, `src/stores/`, `src/contexts/`, `src/lib/`, `src/utils/` | Client interaction/state/presentation behavior; domain-sensitive exceptions require the documented contract |
| Frontend `src/api/` and `src/types/` | Client-side protocol consumption and decoding; your team retains producer/schema authority |
| Frontend component/unit tests, `src/test/`, `tests/e2e/`, visual snapshots | Browser implementation checks; provider/engine acceptance and shared fixtures have explicit coordination obligations |
| Frontend package/lock files, TypeScript/Vite/Playwright/lint configuration and scripts | Frontend toolchain; combined build/runtime/integration concerns are flagged separately |
| `design/`, except `design/ui_kits/website/` | Proposed transfer of the design system, tokens, components and Composer prototypes; this includes generated design outputs, which follow their source ownership |
| `.agents/skills/elspeth-design/SKILL.md` | Maintenance of project UX/design instructions |
| `docs/specs/2026-08-11-composer-desktop-workspace-design.md` | Desktop workspace UX specification |

Their team owns the visible Composer experience. It does not own backend planning or become responsible for deciding whether a pipeline is valid. Ordinary layout/style changes do not require backend approval merely because the same file also contains a flagged protocol behavior.

## What needs deconfliction first

| Decision | Representative exact paths | Proposed resolution |
| --- | --- | --- |
| API schemas, runtime decoders and event vocabulary | `src/elspeth/web/sessions/schemas.py`; `src/elspeth/web/frontend/src/types/api.ts`; `src/elspeth/web/frontend/src/types/guided.ts`; `src/elspeth/web/frontend/src/api/guidedDecoder.ts` | Your team owns the published contract; theirs owns the client. Agree schema generation, exact-key behavior, supported changes and shared fixtures. |
| Duplicated graph and label semantics | `src/elspeth/web/composer/_producer_resolver.py`; `src/elspeth/web/frontend/src/lib/graphTopology.ts`; `src/elspeth/web/composer/guided/_display.py`; `src/elspeth/web/frontend/src/components/catalog/pluginDisplayName.ts` | Keep graph semantics authoritative in backend; choose a single projection/label authority or preserve explicit parity tests. |
| Retry, cancellation, reconnect and authentication | `src/elspeth/web/frontend/src/stores/guidedOperationRetry.ts`; `src/elspeth/web/frontend/src/api/websocket.ts`; `src/elspeth/web/sessions/routes/composer/compose.py`; `src/elspeth/web/auth/routes.py` | Specify behavior across interrupted requests and session recovery; your team keeps authorization and durable operation authority. |
| Tutorial text that affects backend behavior | `src/elspeth/web/frontend/src/components/tutorial/copy.ts`; `src/elspeth/web/frontend/src/components/tutorial/tutorialMachine.ts`; `src/elspeth/web/composer/tutorial_service.py` | Scenario and identity changes need coordination: backend orphan cleanup currently matches a browser-authored pending title. Preserve the same provider-backed path used by ordinary sessions. |
| Application hosting and combined releases | `src/elspeth/web/app.py`; `Dockerfile`; `.dockerignore`; `.github/workflows/build-push.yaml`; `src/elspeth/web/frontend/vite.config.ts` | Your team retains the service/release file owner; theirs supplies frontend build behavior. Decide URL/origin and artifact responsibilities before separate hosting. |
| CI and root JavaScript tooling | `.github/workflows/ci.yaml`; `.github/dependabot.yml`; `package.json`; `package-lock.json`; `.node-version` | Assign jobs/ecosystems, preserving a named integrated release owner. Root Node dependencies mix backend Azurite and browser tooling. |
| Cross-language tests and fixtures | `tests/unit/web/composer/test_graph_topology_parity.py`; `tests/unit/web/composer/guided/test_gate_projection_fixture.py`; `scripts/cicd/bootstrap_proposal_diff_fixture.py`; `src/elspeth/web/frontend/src/api/__fixtures__/gateProposalProjection.json` | Backend owns producer meaning and Python checks; theirs owns client checks. Preserve or publish shared artifacts before moving repositories. |
| Public website remit and tutorial corpus | `website/`; `design/ui_kits/website/`; `.github/workflows/pages.yaml`; `tests/integration/web/test_tutorial_site_pages.py` | Decide whether the team also takes the public site. Published `website/tutorial-site/` inputs are functional test/scenario dependencies. Frontend copies are deliberately prohibited. |
| Mixed user documentation and acceptance | `docs/guides/user-manual.md`; `docs/guides/composer-training-one-hour.md`; `docs/release/composer-guide.md`; `evals/composer-standard-battery/battery.md` | Assign an editor/acceptance owner; theirs owns interface guidance and journeys, yours owns behavior, audit/security claims and expected engine outcomes. |

The table is a reading guide. The linked detailed list and CSV contain the complete preliminary exception set, including counterpart files and tests.

## How to interpret completeness

The **path accounting is exhaustive for the tracked snapshot**: Git supplied all 5,302 paths; CSV readback and the three disjoint lists were checked for duplicates, omissions and nonexistent paths. Untracked files, including this investigation's documents, and ignored build/environment files are outside that snapshot. These new investigation documents are yours.

The **coupling review is preliminary**. Most retained files are explicitly marked `retained-default; not individually reviewed`; frontend defaults inherit the proposed directory ownership. Selected API, UI, build, documentation and test seams were inspected. Design/site scope assignments use representative directory review. Absence from the deconfliction list is not proof of independence, and associated tests inherit the same semantic coordination duties.

This is a proposed allocation. No product code was moved, ownership enforcement changed, or runtime tests executed.
