# 01 — Discovery Findings (Holistic Assessment)

**Pinned tree:** `release/0.8.1` @ `85ebf2739`, read from the detached worktree
`.claude/worktrees/arch-analysis-pin` (the main checkout moved from
`780ef0f56` to `85ebf2739` during the scan because a sibling session merged, so
every agent reads the pin, not the live checkout).
**Date:** 2026-09-23. **Baseline document:** `ARCHITECTURE.md` (last updated
2026-09-11), the stated architecture this as-built analysis is compared against.

## 1. What the system is

ELSPETH is an auditable Sense/Decide/Act pipeline engine. A pipeline is a DAG:
**sources → transforms/gates/barriers → sinks**. Every row, token, node state,
external call, routing decision and sink effect is recorded in the **Landscape**
audit database. There are two authoring surfaces over one runtime:

1. **Version-controlled YAML** run through the `elspeth` CLI (Typer); and
2. the **Web Composer** (FastAPI + React). An LLM tool loop authors the pipeline
   graph, and server-side validation, custody and advisor gates check it.

Both surfaces share the plugin contracts, graph validation, executor, Landscape,
and run accounting.

## 2. Measured size

Instrument: `find … -name '*.py' | xargs cat | wc -l` at the pin. (Validation
found that several rows had been measured at the pre-pin commit `780ef0f56`.
They are now re-measured at `85ebf2739`; see Validation corrections.)

| Area | Lines | Notes |
|---|---:|---|
| `src/elspeth/` production Python | **489,459** (875 files) | ARCHITECTURE.md claims ~455K / 805 files (measured 09-08), so the codebase grew +34K lines / +70 files in 15 days |
| `src/elspeth/web/` Python | 259,872 (360 files) | **53 % of production Python.** ARCHITECTURE.md gives it one container box |
| `web/composer/` | 104,490 | largest single package; `service.py` 11,377, `state.py` 9,064 |
| `web/sessions/` | 58,542 | `service.py` 15,006 (ARCHITECTURE.md says ~14,321, still growing) |
| `plugins/` | 66,811 | |
| `core/` (incl. landscape 38,953) | 65,708 | |
| `engine/` | 43,284 | `processor.py` 5,549 |
| `contracts/` | 34,196 | |
| `web/frontend` TS/TSX | 202,658 (597 files) = **69,903 production (259 files)** + 132,755 test (338 files) | not counted in ARCHITECTURE.md at all. *Corrected (critic G1): the first version compared this all-TS figure with test-excluded production Python. The comparable figure is 69,903.* |
| `elspeth-lints/` | 45,589 | custom static-analysis engine (ADR-023) |
| `gateway/` | 10,498 (`src` 3,230 production; `tests/` 5,757; conformance/mock/scaffold 1,511 test & dev tooling; corrected per critic-2 N1) | standalone LLM compatibility gateway, separate package |
| `scripts/` | 21,121 (`.py` only) | gates, including the canonical 3 scripts |
| `evals/` | 7,028 (`.py` only) | original 7,219 not reproducible at either commit; see Validation corrections |
| `tests/` | 2,380 `.py` files | unit 1,761 · integration 287 · property 98 · testcontainer 74 · e2e 45 |

## 3. Technology stack

- **Python 3.12+**, uv-managed; SQLAlchemy **Core** (not ORM); pluggy plugins;
  NetworkX DAG; RFC 8785 canonical JSON; tenacity; pyrate-limiter; Textual TUI;
  Typer CLI; OpenTelemetry; structlog; Dynaconf settings.
- **Databases:** SQLite / SQLCipher (dev), PostgreSQL 16 (ADR-041 profile). Two
  stores: the **Landscape** audit DB and the **Sessions** DB (composer/auth).
- **Web:** FastAPI, React + TypeScript + Vite frontend, Playwright E2E.
- **LLM providers:** Azure OpenAI, OpenRouter, AWS Bedrock, and the gateway
  (via LiteLLM policy).
- **Deploy targets:** Docker Compose, AWS ECS/Fargate (Aurora PG, EFS, Cognito,
  Bedrock), Azure Container Apps (deferred), systemd, K8s BYO.

## 4. Entry points (`pyproject.toml [project.scripts]`)

| Command | Target |
|---|---|
| `elspeth` | `elspeth.cli:app` (run, validate, resume, explain, doctor, web, composer users …) |
| `elspeth-mcp` | `elspeth.mcp:main`, the read-only Landscape analysis MCP server |
| `elspeth-composer` | `elspeth.composer_mcp:main`, the composer-as-MCP surface |
| `check-contracts` | `scripts.check_contracts:main` |
| pytest11 plugin | `elspeth.testing.pytest_xdist_auto` |
| web app | `elspeth.web.app` (FastAPI factory, launched by `elspeth web`) |

## 5. Organisation

**Layered in the core, feature/domain-organised in `web/`.** The intended
import hierarchy (ARCHITECTURE.md:874) is `contracts (leaf) → core →
engine / plugins / telemetry → UI (cli, tui, mcp, web)`. ADR-006 states it
differently: a strict 4-layer `contracts (L0) → core (L1) → engine (L2) →
plugins (L3)`, which puts plugins *above* engine, so a plugins → engine import
points downward and is permitted there. The two baseline sources disagree on
whether engine and plugins are peers.

### 5.1 Measured import structure (see `temp/import-matrix.md`)

This AST-measured structure is authoritative for every inbound/outbound claim
in later documents.

- **`contracts` IS a true leaf:** 0 outbound rows. Control: `engine → contracts`
  = 340 module-level imports, so the instrument sees contract imports.
- **No upward imports from core/engine:** `core*` → engine/plugins/web = 0 rows;
  `engine` → plugins/web/cli = 0 rows.
- **`plugins.infrastructure → engine` (1 module-level + 1 lazy) and
  `plugins.sinks → engine` (1)** cross the plugins/engine boundary in the
  ADR-006 direction. The S07 slice must adjudicate whether this is allowed.
- **Package-level cycles (SCCs over runtime edges):**
  1. `core ↔ core.landscape ↔ core.checkpoint ↔ core.dag`: core's
     sub-packages are one strongly connected component.
  2. `plugins.{infrastructure, llm, sources, transforms}`: sources import
     transforms (13) and transforms import sources (6 + 3 lazy).
  3. **All 15 `web.*` buckets form one SCC**, even on import-time edges only.
     This holds at the *package* level only. The module-level graph is acyclic
     (X1; see §8.2).
     Heaviest arms: `web.sessions → web.composer` 133 module-level + 55 lazy,
     against `web.composer → web.sessions` 19 + 8. Also
     `web.coordination ↔ web.sessions` (40 each way) and
     `web.execution ↔ web.composer` (23 / 16).
- **UI → web inversions:** `cli → web.*` (22 lazy), `cli_plugins → web.catalog`
  (2 module-level), `composer_mcp → web.composer/execution/catalog/plugin_policy/(root)`
  (18 module-level + 1 lazy; the four named sub-packages alone are 17). The CLI and the composer MCP server depend on the web package as a
  library.

## 6. Subsystem identification

The baseline names 11 containers. The as-built tree has **25 cohesive slices
(S01–S25, frontend split into 3) plus 4 cross-cuts (X1–X4, X2 run as two
agents)** (partition in `00-coordination.md`), because the web tier
alone decomposes into ≥10 domains (composer core loop, composer tools,
advisor/custody/redaction, guided lane, sessions domain, session routes,
execution, coordination, auth/identity, supporting domains, deployment
acceptance) plus a frontend with ~70K production lines (~200K counting its tests).

## 7. Known context the analysis must respect

- **Guided mode is being retired** (maintainer ruling, 2026-09-22): freeform is
  the only authoring mode. The tutorial stays on the shared backend and remains
  a fixed-script canary (ADR-031/049). The guided lane (~21K composer lines plus
  sessions helpers) is a removal-inventory target, not a design to extend.
- **Composer invariants** (AGENTS.md): no server-authored pipeline structure,
  no tutorial-special paths. Two prior violations (`b073d248e`, `9700470e2`)
  evaded a per-walk gate.
- **Trust-tier CI red is a deliberate fail-closed state** (judge-signature
  stage), not a regression.
- **Existing evidence to consume rather than rediscover:**
  `docs/reviews/2026-09-23-release-0.8.1-web-review/` (79 issues, pinned
  74c0ce0db), `docs/arch-analysis-2026-09-07-web-split/`, the ~20 dated reviews
  in `docs/reviews/`, the retired code index index (738 findings; last analyze run
  **failed**, indexed near `ee04378f8`, so treat it as slightly stale and
  secondary to measurement), and the legacy issue tracker (MCP timed out, so use
  the CLI).

## 8. Initial observations (hypotheses for the explorers to confirm or kill)

1. The baseline document under-describes the system's centre of mass: 53 % of
   production Python plus the 69,903-line production frontend is "Web app +
   Composer", which gets one C4 container box.
2. ~~The web tier has no internal layering (one 15-bucket SCC).~~
   **Refined by X1 (`temp/cross-X1-dependencies-layering.md`, measured):** the
   import-time *module* graph is **acyclic**: 875 modules, 5,460 edges, 0
   SCCs. The check used two implementations, and a mutation that added one
   reverse edge produced a 25-module SCC, which confirms it can detect
   cycles. The package-level SCC appears only when modules are bucketed by
   package: the web packages are vertical slices, each spanning roughly 25–33
   levels of the module DAG. Grouped by *role* (domain < service < routes <
   app), web is 98.3 % layered already, with only 33 of 1,931 edges pointing
   upward. The runtime cycles are held together by 41 lazy cycle-breaking
   imports; the other 660 lazy imports break no cycle. Only
   contracts < core < engine is machine-enforced (lint L1). The real debt is
   package boundaries that do not follow the role layering, plus an
   unenforced top layer. "No internal layering" was wrong.
3. Large files keep growing (`sessions/service.py` 14,321 → 15,006;
   `composer/service.py` 10,298 → 11,377) against the baseline's "Medium"
   priority improvement item.
4. The core/engine layering claim holds (measured), apart from 2–3
   plugins→engine edges and intra-core cycles.

## Validation corrections

Independent validation, 2026-09-23, against the pin `85ebf2739`
(`.claude/worktrees/arch-analysis-pin`, `git status` clean). Full report:
`temp/validation-01-discovery.md`. The table below gives the old and new value
for each cell corrected above. Unless noted otherwise, the instrument is
`find <dir> -name '*.py' -not -path '*/__pycache__/*' -not -path '*/frontend/*' -print0 | xargs -0 cat | wc -l`.
It was cross-checked with `git show <commit>:<file> | wc -l` summed over
`git ls-tree` at both `780ef0f56` and `85ebf2739`.

| # | Location | Was | Now (at pin) | Cause |
|---|---|---|---|---|
| C1 | §2 `web/composer/` | 103,642 | 104,490 | measured at `780ef0f56` (git-object count there = 103,642 exactly) |
| C2 | §2 + §8.3 `composer/service.py` | 11,317 | 11,377 | measured at `780ef0f56` (= 11,317 exactly) |
| C3 | §2 `contracts/` | 34,095 | 34,196 | measured at `780ef0f56` (= 34,095 exactly) |
| C4 | §2 `scripts/` | 21,113 | 21,121 | measured at `780ef0f56` (= 21,113 exactly) |
| C5 | §2 `tests/` file counts | 2,375 · unit 1,757 · integration 286 | 2,380 · unit 1,761 · integration 287 | measured at `780ef0f56` (`git ls-tree` = 2,375/1,757/286 exactly) |
| C6 | §2 `web/` | ~259,600 | 259,872 (360 files) | approximate figure matches neither commit (pre-pin 258,954) |
| C7 | §2 `evals/` | 7,219 | 7,028 (`.py`) | not reproducible: `.py` = 7,028 at both commits, `.py`+`.sh` = 7,564; the original instrument is unknown |
| C8 | §5.1 composer_mcp → web | "composer/execution/catalog/plugin_policy (18)" | those four buckets = 10+4+2+1 = **17** module-level; 18 only with `web.(root)` (1 module-level + 1 lazy) | bucket list and number did not agree |
| C9 | §6 partition | 24 slices + 3 cross-cuts | 25 slices (S01–S25) + 4 cross-cuts (X1–X4) | `00-coordination.md` table was revised after advisor review (frontend ×3, X4 added); line 94 of the execution log has the same stale count |
| C10 | §5 hierarchy attribution | attributed peer `engine / plugins` to both ARCHITECTURE.md and ADR-006 | ARCHITECTURE.md:874 = peers; ADR-006:14-17,80 = plugins L3 above engine L2 | the two baseline sources disagree; S07 should adjudicate against both |

**Header claim contradicted by the original table:** the header said every
agent reads the pin, but C1–C5 were byte-exact matches for `780ef0f56`. None of
these corrections changes a conclusion. The total 489,459 / 875 is correct at
the pin, web is still 53 % (259,872 / 489,459 = 53.1 %), and composer is still
the largest package.

**Verified unchanged (with instrument):** the total of 489,459 / 875;
`web/sessions` 58,542; `sessions/service.py` 15,006; `state.py` 9,064; plugins
66,811; core 65,708; landscape 38,953; engine 43,284; `processor.py` 5,549;
frontend TS/TSX 202,658 / 597, all TS including tests (597 tracked; no `node_modules` in the pin; production alone is 69,903 / 259, per critic G1);
elspeth-lints 45,589; gateway 10,498; property 98, testcontainer 74, e2e 45;
ARCHITECTURE.md 455K/805, last updated 2026-09-11, measured 09-08 (lines 5,
20, 186); every entry point against `pyproject.toml [project.scripts]` and
`pytest11`, with each target present (`cli.py:68 app`,
`mcp/__init__.py:30 main`, `composer_mcp/__init__.py:16 main`,
`scripts/check_contracts.py`, `web/app.py:1239 create_app`, launched by
`cli.py:4792`); the three re-derived matrix rows; all the SCC claims; the guided
lane at 21,460 lines (about 21K); retired code index (738 findings, latest run
`failed`, commit `ee04378f8`); the 09-23 review (79 issue files, pinned
`74c0ce0db`).
