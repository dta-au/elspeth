# Frontend dependency overrides

## Temporary braces depth guard

The frontend locks `braces` to the npm alias
`@dieub/braces-depth-guard@3.0.3-pn.2`. This is a reviewed derivative of
`braces@3.0.3`, not an official upstream release. Its immutable npm integrity
is recorded in `src/elspeth/web/frontend/package-lock.json`.

As checked on 2026-10-03, the official advisory
[GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm)
affects all upstream releases through 3.0.3, with no patched release.
Both Stylelint and OpenAPI TypeScript reach this dependency through
micromatch/fast-glob. Updating just one caller leaves the other path exposed.

The derivative adds the depth guards proposed in
[upstream PR #72](https://github.com/micromatch/braces/pull/72), plus its
fractional-depth and cyclic-parent followups. Parsing bounds combined brace
and parenthesis nesting to 100; compile, expand and stringify also bound
caller-supplied AST traversal. Stricter caller limits remain supported, but
larger limits cannot disable the cap. Ordinary glob and range behavior,
upstream attribution and the MIT license are retained.

The published artifact's runtime files match source commit
`a7c294b0535aec8b206bd36ba3f8dba2a7d989cb` in
[dieub/braces-depth-guard](https://github.com/dieub/braces-depth-guard/tree/a7c294b0535aec8b206bd36ba3f8dba2a7d989cb).
The npm provenance identifies that commit and tag `3.0.3-pn.2`.
The source suite contains the upstream compatibility tests and depth-guard
regressions. Repository regression tests resolve the actual installed
dependency through both consumers and run as part of `npm test`.

This override fixes the excessive-nesting mechanism; an audit result for a
renamed package alone is not proof of remediation. It does not add general
expansion-cardinality or AST-width limits. No audit finding is ignored and
the CI threshold remains `--audit-level=low`.

Retire the override when an official upstream release fixes the advisory or
both consumer paths stop depending on braces. Verify the installed graph,
run both locked npm audits, dependency regressions, frontend tests, type
checks, lint and build before removing it.
