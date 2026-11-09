# D1 full-gate repair — exact failures, scoped tests and reviewed bindings

Read this before implementation. Base is `14fef5e5342ccbd79b4fdd69b276c20f77d66dc1`.
Exclusive repair tree: `/workspace/elspeth/.claude/worktrees/composer-remediation-d1-gates`.
Canonical lane: `/workspace/elspeth/.claude/lanes/composer-remediation-wave1`.
The original wave1 tree is frozen under the serial PostgreSQL gate. Never edit it.
The D2 tree has another agent's three unexecuted test drafts; never edit it.

## Evidence and outcome

The completed backend stage exited 1, with healthy continuous freeze, 71,776
passed, 161 failed, 139 errors, 227 skipped and one xfailed. Full issue records
(including complete trace text) are in `d1-backend-issues.json`; source log/XML
are in `d1-combined-gates/20261109T150428Z-composer-remediation-wave1-68412/`.
Read actual traces before converting preliminary `d1-gate-triage.md` hypotheses
into dispositions. All application production code already has Astra approval;
that is not permission to ignore a newly demonstrated defect.

Repair the Composer test expectations/fixtures and reviewed source bindings
that the actual evidence establishes. Preserve D1's direct refusal/no-op,
post-admission proposal cap, strict provenance, atomicity, cancellation, actor,
audit and custody contracts. Do not weaken production checks to accommodate a
bad fixture. Report an actual production defect to the parent before expanding
the intended test/binding repair scope.

## Included failure groups

- Three cap tests in `test_compose_loop_persistence.py`: preserve exact first-N
  durable publication, real invocation prefix, no denied-call audit, unchanged
  graph, truthful progress, and fail-closed reused IDs. Exercise genuine schema
  and semantic refusal separately. Preserve invalid-at-cap feedback; prove an
  actual ready unbound blob effect consumes the tenth admitted slot with nine
  preceding changes and is denied with ten, without changing blob resources.
- `test_service.py::TestComposerSingleToolCall::test_explicit_approve_intercepts_update_blob_without_mutating_blob`:
  preserve its valid-update/pending-proposal purpose. If the observed missing
  provenance matches the preliminary trace, use actual persisted user-message
  custody and `compose`, not the helper that deletes user_message_id. Assert
  exact unchanged bytes/metadata/intent/receipt/reservation state.
- Both `test_endpoint_affordance.py` unset-endpoint exact dictionaries: prove
  baseline behavior; if the observed extra key is reasoning_effort=low, preserve
  full equality and endpoint omission while correcting that exact expectation.
- Architecture/source-binding groups: `test_web_reserve_reviewed_contracts.py`,
  `test_web_landscape_mutation_fencing.py`, `test_operation_fence_wiring.py`,
  `test_static_direct_writers.py`. Read every distinct failure family. Review
  complete changed producer bodies and actual binding dependencies before
  maintaining any ordinary source/AST pin. Use existing generators where
  available. No blanket refresh, rule suppression, signature edit, new writer
  authority or admission of existing unreviewed sites. Retain and run the real
  mutation/negative controls; include any new delegating helper in the evidence
  or binding if the guard's actual ownership model requires it. Source review
  and a hash match alone do not prove the negative oracle remains meaningful.
- Three `test_composer_against_gateway.py` integration failures with real
  LiteLLM 404s: reproduce on the base and candidate, inspect the actual local
  route/model/settings/requests and preserve the real local ASGI/HTTP round
  trips. Do not blame environment or change provider routing without proof.
  A narrowly justified fixture correction belongs here; a production routing
  defect must be reported before expanding scope. No external provider calls.

Parent separately owns dependency failures: SQLite CLI, Chroma model cache,
Terraform cache and Caddy. Do not install dependencies or change their tests.

## Execution and review

One focused pytest process at a time, explicit `-n 0`, both worktree source roots
in PYTHONPATH and pytest's pythonpath override. Parent permits this bounded
capacity while serial PostgreSQL runs elsewhere; no full suite, all-rule scan,
whole-repository typecheck or concurrent child runners. Coordinate expensive
checks. Never mutate a tree while its own checks are running. Before baseline
probes, coordinate use of the existing clean BASE checkout with parent; do not
switch branches or silently reuse editable-install provenance.

Full-gate failures provide meaningful red evidence; targeted reproductions and
negative controls must still distinguish the intended repairs. Keep exact
commands, exit codes, complete logs and restoration evidence in lane-private
artifacts. Choose relevant whole-tree test/authority gates from actual changed
inputs. Do not merely change counts, remove cases, replace exact assertions with
subset matches, or skip failures. Retain substantive existing controls.

No subagents, commits, push, Page changes or external issue messages. Parent
will review the staged path set and obtain Astra spec/quality review before
committing. Avoid staging other agents' files; this tree starts clean.

Write `d1-gate-repair-report.md` and `d1-gate-repair-review.diff` (including new
files) in the lane. Include all failure dispositions, measured baseline versus
D1 distinctions, exact changed paths and verification limits. Return only paths,
short status, test summary and remaining concerns.
