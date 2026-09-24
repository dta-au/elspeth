# Composer live acceptance battery

Ten synthetic workflows exercise ordinary Composer authoring, edits, interpretation reviews, validation, execution, output contents and audit accounting. The runner never submits or patches pipeline structure. Fixtures contain natural-language requests and data, not canonical graphs. This promotes the public HTTP workflow from the existing `evals/composer-harness/hardmode` harness into tracked, self-contained modules; the ignored historical evaluation archive is not required.

The separate parameterless-tool canary calls every production zero-argument tool using the full tool list, AUTO selection, Together routing and the production wire decoder. It proves wire conformance, not pipeline execution.

The isolated service enables the six optional transforms required by these fixtures: `batch_top_k`, `json_explode`, `keyword_filter`, `truncate`, `type_coerce`, and `value_transform`. All other fixture plugins use the standard mandatory Web policy. This allowlist is recorded in the service configuration and checked against the actual registry and fixtures before boot provider calls. The deployed plugin policy is never changed.

Run from the task checkout with both source roots so editable installs cannot silently test the main checkout:

```bash
ACCEPTANCE_ROOT=$(git rev-parse --show-toplevel)
cd "$ACCEPTANCE_ROOT" && export PYTHONPATH="$ACCEPTANCE_ROOT/src:$ACCEPTANCE_ROOT/elspeth-lints/src:$ACCEPTANCE_ROOT"
cd "$ACCEPTANCE_ROOT" && .venv/bin/python -m scripts.composer_acceptance.runner
cd "$ACCEPTANCE_ROOT" && .venv/bin/python -m scripts.composer_acceptance.serve --output .claude/lanes/composer-battery/example
```

These commands list scenarios or print configuration without loading a key or making paid calls. Output directories should be private and ignored, or outside the checkout. They contain synthetic data, local test login credentials and audit stores. Never stage them.

For live execution, choose a fresh output directory and explicitly provide the authorized environment file. Only its `OPENROUTER_API_KEY` entry is parsed, without interpolation; no deployment environment or other credential is loaded. The profile and Composer defaults match the maintained DeepSeek/GLM/Sonnet test configuration; model IDs can be overridden on the service CLI and must be checked against the provider catalog when changed.

```bash
ACCEPTANCE_OUT="$ACCEPTANCE_ROOT/.claude/lanes/composer-battery/$(date -u +%Y%m%dT%H%M%SZ)"
ACCEPTANCE_ENV=/path/to/authorized/.env
cd "$ACCEPTANCE_ROOT" && .venv/bin/python -m scripts.composer_acceptance.canary --execute --env-file "$ACCEPTANCE_ENV" --output "$ACCEPTANCE_OUT"
cd "$ACCEPTANCE_ROOT" && .venv/bin/python -m scripts.composer_acceptance.serve --execute --env-file "$ACCEPTANCE_ENV" --output "$ACCEPTANCE_OUT" > "$ACCEPTANCE_OUT/service.log" 2>&1
```

Keep the service in its own terminal. Run the canary before starting the service; do not run two provider-ledger owners concurrently. The service binds only loopback and creates its own auth, session, Landscape and blob stores. It does not load a deployed session or change the deployed service. It refuses to resume an output directory if production/fixture/oracle content or effective service configuration changed. The runner checks the source fingerprint before starting and before accepting a pass, and checks custom fixture content too. Stop the service with Ctrl-C after the battery to write the final source fingerprint and budget totals; a changed source tree makes shutdown fail.

In another terminal with the same source path and variables:

```bash
cd "$ACCEPTANCE_ROOT" && .venv/bin/python -m scripts.composer_acceptance.runner --execute --output "$ACCEPTANCE_OUT"
```

Exit 0 means selected cases passed; 1 means at least one failed; 2 means actual interpretation cards await inspection. Each case is independently resumable. Read `cases/<id>/pending-reviews.json`, its fixture's `review_policy.constraints`, and the model's actual response. Confirm that drafts preserve the requested semantics, supplied data, source identity and operator model profile. Broad allowed review kinds never authorize blanket acceptance. For inspected cards only:

```bash
cd "$ACCEPTANCE_ROOT" && .venv/bin/python -m scripts.composer_acceptance.runner --execute --output "$ACCEPTANCE_OUT" --case 05_complaint_sla --accept-reviewed EVENT_UUID --accept-reviewed ANOTHER_EVENT_UUID
```

The runner resolves only those exact event IDs and records each response. Incorrect interpretations remain pending; use an ordinary user correction through the application, then preserve it in the scenario's evidence. Do not resolve an advisor rejection, change an expected output to fit a failure, or submit a helper-authored graph. At most one ordinary repair turn quotes real application blockers; every planned edit, repair and final preview counts toward the fixture's five-turn limit. A model response claiming success is never sufficient.

The global default budget is 200 OpenRouter chat HTTP dispatch attempts and a $10 **known-cost stop** shared by Composer, advisor, boot probes and runtime plugins. It observes actual synchronous and asynchronous HTTP sends without rewriting request bytes. Requests stop before the next dispatch when either threshold is reached. Cost arrives after a response, so an in-flight request can overshoot; concurrent runtime requests can already be in flight. Unpriced failures and cancellations consume the request cap and remain explicit. This is not a hard billing cap. The ledger survives service restarts; do not delete it to reset a run's limits.

Evidence per case includes fixture, input uploads, exact user messages and model replies, in-flight progress, resolved cards, strict validation, final state, provider/tool audit, run accounting, artifact manifest, downloaded output bytes and checker failures. Runtime evidence binds Landscape calls and actual model identities to source rows; token parents and lineage frames prove expansion and fork pairing; physical source hashes prove uploaded data was preserved. Preview must succeed after the final applied edit. `results.json` distinguishes passing, pending review, semantic failure, runtime timeout and harness error. An interrupted execution resumes polling its recorded run; if the HTTP receipt was lost, the runner stops for run-history inspection instead of issuing another execution. Source fingerprints include production code, dependency pins, battery implementation and fixtures. One run per case is a concrete acceptance result, not a statistical convergence claim.

Provider-free controls:

```bash
cd "$ACCEPTANCE_ROOT" && .venv/bin/python -m pytest -n 0 tests/unit/scripts/test_composer_convergence_checks.py tests/unit/scripts/test_composer_acceptance_budget.py tests/unit/scripts/test_composer_acceptance_runner.py
```

The output checker tests mutate known-good results; the budget tests prove sync/async accounting, byte preservation, request caps, post-response overshoot and unpriced failures; lifecycle controls cover exact review approval, interrupted execution resume, worker timeouts, stale or failed preview rejection, changed source/configuration/fixtures, and real SQLite evidence collection.
