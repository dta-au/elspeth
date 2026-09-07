# Initial discovery

The architecture entry point describes CLI and Web Composer authoring against a shared Python engine, plugin contracts, validation, and Landscape audit storage. Its historical inventories are not used as current measurements.

The frontend has its own locked Node package at `src/elspeth/web/frontend/`. The root `Dockerfile` builds it and installs its output alongside the Python web package. `.github/workflows/ci.yaml` has frontend unit/typecheck and browser E2E jobs in the required CI aggregate.

The written compiler proposals are `docs/specs/2026-04-15-compiled-pipeline-architecture-design.md` and `docs/specs/2026-08-20-compiler-facade-mvp-sketch.md`. Their implementation status needs source verification.

The investigation follows three responsibility groups: browser UX; backend application services including Composer, authorization, sessions and API transport; and compiler/runtime/plugin/audit capabilities. These are ownership candidates, not a claim about separate deployed services.
