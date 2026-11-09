# Guidance design clarification round 1

Scoped response to `guidance-parity-design-review.md`. Amended only
`guidance-parity-design.md` and `guidance-parity-implementation-brief.md`;
preserved their preceding bytes as the corresponding `.pre-fix1.md` files.
Review patch: `guidance-parity-design-fix1.diff` (two-file unified diff).

1. **Palette exercise classified accurately.** Re-read
   `pipeline_planner.py:355–397,1652–1674` at
   `14fef5e5342ccbd79b4fdd69b276c20f77d66dc1`: production initialization uses
   the full discovery tuple and manifest updates preserve it. Ordinary route
   coverage stays full-policy. Narrowing is now an explicitly synthetic,
   test-only wrapper around the real initializer, with captured outbound tools;
   production policy must not change to support it.
2. **Exception convention made concrete.** The small test-only checker uses a
   complete declaration/control vocabulary independently of the captured
   available palette. A finite reviewed fixture identifies exact paragraphs,
   anchors, origin, scope kind and permitted names. Missing/stale/duplicate/
   overlapping/ambiguous spans fail closed. Conditional-guard deletion and
   displacement, second unscoped occurrences and stale/duplicate paragraphs are
   explicit enduring controls. No new production metadata/parser/framework.
3. **Preservation made auditable.** Added the old-span → retained/new owner →
   meaningful assertion map, verified against the source policy and existing
   aids. It includes request/assistant history, unread supplied evidence,
   literals/adaptation and A/B constraints, requested topology, descriptions,
   generated-source first-person provenance, review staging, required versus
   recommended controls, failure retention and output minimization. The brief
   requires exact final owner spans/test ids, complete-rule mutation controls
   and applicable production-path controls before overlay replacement. It
   expressly limits scripted evidence to preservation/reachability, not
   prompt-caused live-model compliance.

Also made the separate authority of the imported audit working documents
explicit and adopted separate service-wiring versus exhaustive projection
fixtures sharing one parity assertion. D7/D8 and whole-R8/MCP/freeform closure
boundaries are unchanged; completed 13.16 is preserved.

Verification in this round was read-only source/document inspection and diff
generation. No code/test edits, test or scan execution, provider calls,
subagents, index/HEAD changes or Page updates. No implementation approval or
closure claim is made. Ready for the original Astra reviewer's scoped re-review
of the three clarifications.
