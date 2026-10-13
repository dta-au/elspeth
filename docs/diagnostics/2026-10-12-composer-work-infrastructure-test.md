# Composer test candidate for work infrastructure

Branch: [`codex/emergency-diagnostics-20261012`](https://github.com/dta-au/elspeth/tree/codex/emergency-diagnostics-20261012), [PR #296](https://github.com/dta-au/elspeth/pull/296). Use the exact candidate SHA supplied in the publication message. This is an operator-run **test candidate** with the qualification limits below.

## Qualification and remaining work

| Evidence | What it establishes | Limit |
|---|---|---|
| Chained nested-reference deterministic acceptance: 2 tests passed, exit 0 | Four real uploads, canned model responses through normal Composer finalization, strict validation, three nested reference resolutions and real execution; 3 exact successes and 1 original-only quarantine; wrong-hash execution refused | Model boundary and test identity were fixtures. Executed on the frozen chaining package based on `8007aa5d4`; all 14 reviewed files match the subsequently published `7baa9bccc08d164a061e8735fdf2d681999bcebb`. The final follow-up changes only test pins and this document |
| Earlier independent multi-CSV live acceptance | Real model authored and executed three independent reference lookups | Older candidate; references were inline copies. Does not prove chained model authoring or nested upload binding |
| Live chained acceptance | **BLOCKED / PARKED**: three startup HTTP 402 responses, no authoring or execution | John confirmed account exhaustion. No automatic retries or resumption; saved responses do not contain provider response bodies |
| Required CI | **Pending landing qualification** | Final combined-head required checks, including default-selection and PostgreSQL gates, remain required. Check the publication message and PR for current status; this candidate is not cleared for merge or release |
| Local authority gate | Completed focused qualification: **12 passed, 1 existing xfail, exit 0**, 149.72 seconds; Ruff check/format passed | Four stale line-pin failures repaired by 15 test-pin changes; production unchanged. Moved-block, multiplicity, wrong-authority, raw-connection, ingress rename/insertion and settlement mutation controls passed. The preceding broad selection was **2,194 passed, 4 failed, 2 skipped, 1 xfailed**, not green. Focused repair evidence does not certify pending required CI; the xfail is not a pass |

The ignored local acceptance test and its receipts are not shipped by cloning this branch. They are supporting development evidence, not a runnable file promised to the workplace operator. The shipped YAML/CSV example below is the reproducible runtime control.

## 1. Obtain the exact candidate

Use a new, isolated test checkout and test data stores. These commands do not update an existing service or its databases:

```bash
git clone --branch codex/emergency-diagnostics-20261012 --single-branch https://github.com/dta-au/elspeth.git elspeth-work-test
cd elspeth-work-test
read -r -p "Candidate SHA from publication message: " candidate_sha
git checkout --detach "$candidate_sha"
git rev-parse HEAD
git status --short
```

Record the printed SHA. Stop if the requested commit is unavailable; obtain the integration owner's published replacement pin instead of treating a moving branch tip as the same candidate. Keep this candidate detached while testing.

Prerequisites: Python 3.12+, `uv`, Node 24 and npm 11. Python 3.12 was used for the deterministic proof. From this checkout:

```bash
uv sync --frozen --extra webui
npm --prefix src/elspeth/web/frontend ci
npm --prefix src/elspeth/web/frontend run build
uv run python -c 'import elspeth; print(elspeth.__file__)'
```

The printed import path must belong to this checkout. `webui` includes Composer's LiteLLM dependency; lint tooling is unnecessary for this runtime-only installation. Provider/deployment-specific extras, such as `aws`, `azure` or `postgres`, are operator choices governed by the selected deployment runbook; this handoff does not select an infrastructure target.

## 2. Supply the approved workplace configuration

The operator supplies the existing approved provider, model routes, endpoints, credentials, authentication, data location and spending policy. Do not copy a development `.env`, use a colleague's token, or assume the parked OpenRouter account is available. Configure the service through the workplace's normal secret/configuration mechanism. No credential values or model/endpoint values are supplied here.

| Purpose | Supported variable names |
|---|---|
| Primary and mandatory advisor routes | `ELSPETH_WEB__COMPOSER_MODEL`, `ELSPETH_WEB__COMPOSER_ADVISOR_MODEL` |
| Optional separate pricing identities for deployment aliases | `ELSPETH_WEB__COMPOSER_PRICING_MODEL`, `ELSPETH_WEB__COMPOSER_ADVISOR_PRICING_MODEL` |
| Optional custom primary OpenAI-compatible endpoint and paired credential | `ELSPETH_WEB__COMPOSER_ENDPOINT_BASE_URL`, `ELSPETH_WEB__COMPOSER_ENDPOINT_API_KEY` |
| Optional custom advisor endpoint and paired credential | `ELSPETH_WEB__COMPOSER_ADVISOR_ENDPOINT_BASE_URL`, `ELSPETH_WEB__COMPOSER_ADVISOR_ENDPOINT_API_KEY` |
| Direct OpenAI / Anthropic / OpenRouter credentials, according to selected route | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY` |
| Direct Azure credential alternatives / Azure AI | `AZURE_API_KEY`, `AZURE_OPENAI_API_KEY`, `AZURE_AD_TOKEN`, `AZURE_AI_API_KEY` |
| Azure API-version selection, if applicable | `AZURE_API_VERSION` |
| Explicit same-model advisor exception, only if chosen by operator | `ELSPETH_WEB__COMPOSER_ALLOW_SAME_ADVISOR_MODEL` |
| Tool-contract compatibility and normal startup conformance probes | `ELSPETH_WEB__COMPOSER_STRICT_TOOLS`, `ELSPETH_WEB__COMPOSER_BOOT_PROBE_ENABLED` |
| Required authoring bounds | `ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS`, `ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS`, `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS`, `ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE` |
| Additional cost, call and transport bounds | `ELSPETH_WEB__COMPOSER_PLANNER_MAX_PROVIDER_CALLS`, `ELSPETH_WEB__COMPOSER_PLANNER_MAX_CUMULATIVE_PROVIDER_COST`, `ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS` |
| Web signing secrets | `ELSPETH_WEB__SECRET_KEY`, `ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY` |
| Authentication and admission | `ELSPETH_WEB__AUTH_PROVIDER`, `ELSPETH_WEB__REGISTRATION_MODE` |
| Isolated persistence and deployment selection | `ELSPETH_WEB__DATA_DIR`, `ELSPETH_WEB__PAYLOAD_STORE_PATH`, `ELSPETH_WEB__SESSION_DB_URL`, `ELSPETH_WEB__LANDSCAPE_URL`, `ELSPETH_WEB__DEPLOYMENT_TARGET`, `ELSPETH_WEB__DEPLOYMENT_STATE_MODE`, `WEB_CONCURRENCY` |

For Bedrock, use the operator-approved ambient AWS credential chain; do not introduce static AWS keys or endpoint overrides as part of this test. For custom endpoints, each role needs its own endpoint/credential pair; neither inherits the other's settings, and configuring an endpoint does not rewrite the route's model identifier. Primary and advisor models must differ by default. An explicit same-model exception preserves advisor calls but accepts correlated model blind spots; this handoff does not select it.

Startup makes provider conformance calls and can incur cost. Start only with workplace authorization for the selected routes and spending bounds. Keep the ordinary boot probes enabled for the live test. A healthy web server does not establish successful probes or working Composer. Unknown pricing is unavailable cost, not zero cost. The previous development driver had its own 30-attempt / USD 3 known-cost / 900-second guard; those are not automatically installed by these ordinary web startup commands, nor a guarantee against unpriced billing.

**The LLM authors the pipeline through Composer; the actual lookups are local CSV `reference_join` operations. This case does not test a runtime LLM lookup/transform.** It needs no runtime `llm` profile. If testing a separate LLM transform, the operator must additionally configure `ELSPETH_WEB__LLM_PROFILES`, `ELSPETH_WEB__DEFAULT_LLM_PROFILE`, applicable `ELSPETH_WEB__SERVER_SECRET_ALLOWLIST` / `ELSPETH_WEB__USER_SECRETS_ENABLED`, and `ELSPETH_FINGERPRINT_KEY` for server-scoped credential use. That is a separate test and spending scope.

## 3. Start an isolated Composer and admit the tester

For a new single-host test instance, after the operator has supplied the configuration above, the repository's supported loopback command is:

```bash
uv run elspeth web --host 127.0.0.1 --port 8451
```

Keep this foreground service running. Run the remaining CLI commands in a second shell with the same approved configuration and checkout. Open `http://127.0.0.1:8451` locally. A remote workplace deployment must use its approved ingress, TLS, authentication and persistence runbook; do not expose this loopback recipe by merely changing the bind address. Keep one web worker. External PostgreSQL deployments require explicit schema initialization under the deployment runbook; do not run schema-reset/recreation commands against existing workplace stores.

If the operator selects local authentication for this fresh instance, create credentials in the same configured data directory (the shell expansion below requires it to be explicitly set):

```bash
uv run elspeth composer users add work-test-admin --display-name "Work Test Admin" --data-dir "${ELSPETH_WEB__DATA_DIR:?Set the isolated test data directory}"
uv run elspeth composer users add work-test-user --display-name "Work Test User" --data-dir "${ELSPETH_WEB__DATA_DIR:?Set the isolated test data directory}"
```

These commands prompt for unique passwords; never pass them on the command line. Account creation does not grant workload access. Only on a fresh deployment with no active administrator, the operator can bootstrap the first local administrator using the normal audited command:

```bash
uv run elspeth composer users bootstrap-admin local work-test-admin --data-dir "${ELSPETH_WEB__DATA_DIR:?Set the isolated test data directory}" --note "Initial administrator for isolated workplace candidate testing"
```

If the deployment has an existing administrator or uses SSO, use that administrator and the configured provider's normal admission flow instead. Have the tester attempt one ordinary sign-in to establish its identity. Under non-open registration, it awaits administrator activation: the administrator activates it and grants its deployment-wide `user` workload role through People & access / the normal authenticated identity administration API, after which the tester signs in again. With explicitly selected open registration, verify the ordinary admission and role outcome instead. Alternatively, use the normal administrator pre-provisioning flow. An `admin` grant alone does not authorize pipeline authoring/execution; keep the test user separate. Supply any required quota and review/approver assignments through the same ordinary administration workflow. Do not alter rows directly or replace authenticated identity checks.

## 4. Run the shipped runtime control

In the isolated checkout, capture the command and exit without piping the process output:

```bash
mkdir -p .claude/lanes/work-test
uv run elspeth run --settings examples/reference_join/settings_chained.yaml --execute > .claude/lanes/work-test/chained-cli.log 2>&1
chain_status=$?
printf 'exit=%s\n' "$chain_status"
```

Expected: **PARTIAL and exit 1 by design**, because one row is routed to quarantine. Inspect the completed log, `examples/reference_join/output/chained_enriched.csv`, `examples/reference_join/output/chained_quarantine.csv` and `examples/reference_join/runs/chained_audit.db`. A different error also exits nonzero, so exit 1 alone is not acceptance. Do not repeatedly append runs and then count all database rows as one run; retain the current run ID.

## 5. Ask the real Composer to author the chain

Sign in as the admitted tester, create a fresh ordinary session and upload these four shipped files as separate files:

- `examples/reference_join/orders_chained.csv` — the source.
- `examples/reference_join/product_aliases.csv` — alias reference.
- `examples/reference_join/products.csv` — product reference.
- `examples/reference_join/fulfilment.csv` — warehouse reference.

Use this natural-language request; do not paste a prebuilt graph into state or use a server-authored fallback:

> Build a CSV-to-CSV pipeline from my four uploads. The orders file is the only source. Keep order_id, product_alias and quantity as strings. Use one reference_join with three ordered lookups: first match product_alias to alias in product_aliases.csv and return sku as product_code. If the alias is missing, default product_code to the string socks. Next use that product_code to match sku in products.csv, adding description as product_description and unit_price as unit_price. Finally use the same product_code to match sku in fulfilment.csv and add warehouse. Fail the whole row on a missing product or warehouse. Route successful rows to one CSV sink and any failed row to a separate quarantine CSV sink, preserving exactly the original three source fields in quarantine, with no partial enrichment. Bind the uploaded reference files, retaining their upload identity and content hashes rather than retyping their contents. Use local CSV processing only; no runtime LLM transform, network lookup or new credentials. Ask me to clarify anything this does not specify before proceeding.

Keep `product_code`: names ending in `_key`, including `product_key`, can trigger the existing credential-name guard. Do not relax that guard to make this example pass.

Inspect the actual proposed lookups, schemas, miss policies and sink routing. Answer clarification/review cards only against the stated contract. Do not waive a mismatch or claim an agent response is human approval. Use normal review, validation and execute controls. If an unresolved card needs a workplace decision, obtain that decision. If the model adds an external plugin, changes defaults, or cannot author the chain, retain the failed attempt rather than repairing the stored graph by hand.

For the nested-upload proof, verify actual saved markers under `options.tables.<table>.reference_content` and their three run-bound `blob_inline_resolutions`. An authored inline copy can demonstrate functional chaining if outputs match, but cannot pass the upload-binding/materialization portion. Record that limitation rather than relabelling it.

## 6. Exact result contract

Compare CSVs as records with exact string values; column order and quoting do not matter. Every successful record has exactly these seven fields:

| order_id | product_alias | quantity | product_code | product_description | unit_price | warehouse |
|---|---|---|---|---|---|---|
| C-1001 | sunhat | 2 | hats | Wide-brimmed sun hat | 25.00 | Canberra |
| C-1002 | unknown | 3 | socks | Merino wool walking socks | 18.50 | Melbourne |
| C-1004 | raincoat | 1 | coats | Waxed canvas field coat | 180.00 | Sydney |

Quarantine must contain exactly one record and exactly these original three fields:

```json
{"order_id":"C-1003","product_alias":"driving-gloves","quantity":"1"}
```

No `product_code`, `product_description`, `unit_price` or `warehouse` may appear in that quarantine row. The failure occurs after earlier lookups succeeded: Landscape must attribute `reference_miss` to lookup index `2`, table `fulfilment`, input key field `product_code`. The defaulted C-1002 must show that `socks` drove both later matches. Check four source rows and four terminal outcomes: three successes and one routed failure. Web execution is expected to finish `completed_with_failures`, corresponding to the intended PARTIAL CLI result.

Null and other boundary cases are separate checks; the shipped four-row scenario does not exercise null:

| Additional operator-authored variant | Expected behavior |
|---|---|
| Change the alias miss policy to null and remove the default | C-1002 produces an intermediate null; its products consumer fails `invalid_input` at lookup index 1. The complete original C-1002 row is quarantined, even if that consumer itself uses null/default miss handling |
| An unconsumed terminal output misses under null policy | That output can be null in a successful row; inspect typed runtime/audit data because a blank CSV cell alone does not prove null rather than an empty string |
| A configured default string is absent from the next table | The consumer applies its own miss policy to that string; a default is not an automatic success of downstream lookups |
| A later lookup references its own or a future output, duplicates output names, or overwrites source fields | Configuration/validation must reject it; do not execute a rejected graph |

Keep each variant in a fresh session or explicitly identified revision. Do not apply the base fixture's three-success expectation to a changed policy.

## 7. Capture useful, safe evidence

Record candidate SHA, dependency/tool versions, approved nonsecret provider/model route identity, configuration **names**, UTC attempt times, session/operation/run IDs, actual review decisions, strict validation result, terminal run accounting, exact downloaded synthetic CSV records and exit codes. Record requested/returned/pricing model identities separately where available. Report all failed attempts and provider costs as known-priced subtotal plus count of unpriced attempts; never convert missing costs to zero.

For nested uploads, retain upload IDs, byte counts/content hashes, the three saved nested binding paths, their resolution records, all four run-input links and the runtime's materialized reference contents. This distinguishes actual binding from model-copied CSV. Preserve ordinary Landscape provenance and review events; if there were no review cards, say so.

Do not attach `.env`, auth/session databases, raw bearer headers/cookies, signed links, provider keys, service configuration containing credentials, browser storage, or a full process environment. Keep raw diagnostic/audit stores inside the approved workplace boundary; export only the minimal reviewed/redacted subset. Synthetic fixture data is safe to include, but surrounding logs can contain private configuration and identities. Report sanitized provider response text if retained; do not invent a response body or infer billing details solely from a status code.

Stop the owned foreground service with Ctrl-C and record its exit after collecting evidence. Provider/account failure means a blocked live test, not a passing engine test or permission to retry with another provider. The parked development OpenRouter attempt remains parked; this handoff does not resume it.

## References and remaining operator decisions

Commands and settings were checked against the [local Composer guide](../guides/web-local-development.md), [environment reference](../reference/environment-variables.md#web-llm-configuration), [identity provider guide](../guides/identity-providers.md), CLI/configuration implementation, and [reference lookup example](../../examples/reference_join/README.md) at `7baa9bccc08d164a061e8735fdf2d681999bcebb`. Those production files are unchanged by the test-pin follow-up. The [combined repair handoff](2026-10-12-combined-repair-handoff.md) and [Fable review](2026-10-12-fable-orchestrator-review.md) describe the diagnostic changes and review limits.

The operator chooses workplace deployment and storage, provider/model/endpoint pairing and pricing identities, credential source, authentication/admission and review assignments, spending/time limits, and additional policy variants. The publication message supplies the verified commit and current CI status. Preparation of this handoff did not access or configure workplace infrastructure or invoke a provider.
