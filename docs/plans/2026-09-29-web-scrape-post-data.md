# Web Scrape POST Data Implementation Plan

> **Implementation handoff:** Execute the tasks in order, with a failing behavior test before each production change. This is a plan, not authorization to change the plugin.

**Goal:** Let `web_scrape` fetch data with an audited, SSRF-safe HTTP POST while preserving its existing GET behavior.

**Architecture:** Add an explicit method and a row-field reference for a JSON request body to `WebScrapeConfig`. Build and validate that body before DNS resolution or egress, then use `AuditedHTTPClient.request_ssrf_safe()` for POST. Keep the existing GET path. A POST does not follow redirects in this first version; the existing shared redirect loop always converts a followed request to GET, including for 307/308. Feed successful responses into the current extraction and fingerprint path, with `format: raw` for JSON responses.

**Tech stack:** Python 3.12, Pydantic, httpx, `AuditedHTTPClient`, pytest/respx, the plugin catalog golden and CI source-hash gates.

**Planning base:** `release/0.8.1` at `63906a374`. Confirm the implementation target branch before changing code or release documentation.

**Prerequisite:** Create an isolated implementation worktree with its own Python environment, export both the worktree `src` and `elspeth-lints/src` roots in `PYTHONPATH`, and verify both packages import from that worktree before running tests. Do not modify the shared checkout's unrelated changes.

## Proposed user contract

```yaml
transforms:
  - plugin: web_scrape
    options:
      url_field: endpoint_url
      content_field: response_text
      fingerprint_field: response_fingerprint
      format: raw
      method: POST
      request_json_field: query_body
      http:
        abuse_contact: ops@example.org
        scraping_reason: Public data retrieval
        allowed_hosts: public_only
      schema: {mode: observed}
```

- `method` is `GET` or `POST`, defaulting to `GET`. Existing configurations and GET request evidence retain their current shape.
- `request_json_field` is required for POST and forbidden for GET. Its value names one arriving row field holding a JSON object. No implicit selection of the entire row occurs. Empty JSON objects are valid; absent, non-object, or non-JSON values are row errors before network access.
- Bound the serialized request body before egress (proposed default: 1 MiB via `http.max_request_body_bytes`); report only field name, type, and size in row errors, never body values. Reject non-string object keys, unsupported values, non-finite numbers, and an oversized body. The emitted body must match the body whose size was checked.
- POST sends `Content-Type: application/json` through the shared client. No credentials, custom request headers, form encoding, or multipart bodies are added by this extension. The request JSON is stored in the audit call and replay evidence, so the operator must not put secrets in this field.
- A POST 3xx is a nonretryable row error, with the initial response still audited. GET keeps its existing redirect behavior. This avoids both an unrequested second POST and the current shared client's GET rewrite on all redirect status codes.
- POST failures are not automatically retried by the transform, including 408, 429, 5xx, timeout, and connection loss: after an ambiguous failure the remote endpoint may already have processed the request. Route them through `on_error` with a value-free status or error category. Document that a whole-run restart can still issue another POST, so this feature is for data-retrieval endpoints whose repeated calls are safe; it is not a general side-effecting API client.
- `text/*` and `application/xhtml+xml` keep the current extraction behavior. Accept `application/json` only when `format: raw`; emit the response as a string so an existing downstream JSON transform can parse it. Other non-text media types remain errors. The output fields and content fingerprint semantics remain unchanged.

## Task 1 — Pin the configuration contract

**Files:** `src/elspeth/plugins/transforms/web_scrape.py`; `tests/unit/plugins/transforms/test_web_scrape.py`; `tests/unit/plugins/test_validation_path_agreement.py` if its config-rejection inventory requires a new case.

1. Add failing tests for default GET, valid POST with `request_json_field`, POST without it, GET with it, empty field name, and an input field that collides with a field created by this transform. Assert `declared_input_fields` includes the POST body field, so fixed and flexible schema checks can see it. Update the hermetic forward-invariant probe to supply `{}` in that field when configured for POST; add a POST probe test.
2. From the implementation worktree, run `cd "$(git rev-parse --show-toplevel)" && PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" .venv/bin/python -m pytest tests/unit/plugins/transforms/test_web_scrape.py -n 0` and record the nonzero exit code from the new tests.
3. Add `method: Literal["GET", "POST"] = "GET"` and `request_json_field: str | None = None` to `WebScrapeConfig`; use a model validator for the method/field relationship. Add `http.max_request_body_bytes` with a positive bound. Add the field to `declared_input_fields` and to `_reject_input_options_naming_created_fields` only when configured. Store validated settings in the transform.
4. Rerun those nodes and the complete `tests/unit/plugins/transforms/test_web_scrape.py` file. Success means all old GET tests still pass and every invalid combination is rejected at plugin validation.

## Task 2 — Validate each POST body before egress

**Files:** `src/elspeth/plugins/transforms/web_scrape.py`; `tests/unit/plugins/transforms/test_web_scrape.py`; `tests/unit/plugins/transforms/test_web_scrape_security.py`.

1. Add failing process tests for a missing body field, list/scalar values, nested non-JSON values, non-finite numbers, and an oversized JSON object. In each case assert a nonretryable `TransformResult.error`, zero DNS calls, zero HTTP calls, and no body value in the reason.
2. Add a focused body parser at the row boundary. Accept a JSON object with string keys and recursively valid JSON values; detach it from the row before passing it to the client. Calculate the same serialized byte count that the outbound `httpx` JSON path sends. Keep invalid row data out of exception messages and logs.
3. Run the focused tests, then `tests/unit/plugins/test_process_path_type_error_gate.py` and the relevant transform contract test. Success means malformed row data cannot escape `process()` as a Python exception or reach the network.

## Task 3 — Wire audited POST and replay/verify

**Files:** `src/elspeth/plugins/transforms/web_scrape.py`; `tests/unit/plugins/transforms/test_web_scrape.py`; `tests/unit/plugins/transforms/test_web_scrape_security.py`; `tests/unit/plugins/clients/test_http.py` only if an actual shared-client defect is found.

1. Add failing tests that inspect the IP-pinned request: method POST, JSON bytes, responsible-scraping headers, response cap, and one recorded `HTTPCallRequest` whose `json` matches the sent object. Assert a POST redirect is recorded but never followed. Include a malicious private redirect target as a negative control: no second network call occurs. Assert POST 408, 429, 5xx, and ambiguous network failure return nonretryable row errors without leaking request values, while the same GET cases keep their current retry behavior.
2. Add replay and verify tests with two requests to the same URL but different JSON bodies. Replay must select matching evidence and make no network request. Verify must reject a body mismatch before live DNS/egress, and accept a matching body. Confirm the request body is part of `HTTPCallRequest` in both the pre-DNS verify request and the live audited request.
3. In `process()`, build the body before the existing replay/verify/SSRF flow. For POST, use `client.request_ssrf_safe("POST", safe_request, json=body, follow_redirects=False, allowed_ranges=...)`; keep `get_ssrf_safe(...)` for GET to preserve its established behavior. Handle POST 3xx through the existing unresolved-redirect error path, and convert POST retryable errors to nonretryable row results before they reach the engine retry manager. Keep `_fetch_url` and its hermetic probe fake in signature agreement when the body argument is added.
4. Rerun focused transform, security, and client tests. Success means the POST request, audit, replay, and verify all agree on method and body; GET evidence remains unchanged.

## Task 4 — Accept raw JSON responses and expose the option

**Files:** `src/elspeth/plugins/transforms/web_scrape.py`; `docs/reference/web-scrape-transform.md`; `tests/unit/plugins/transforms/test_web_scrape.py`; `tests/unit/plugins/transforms/test_external_catalogue_metadata.py`; `tests/golden/web/catalog/knob_schema/transform__web_scrape.json`.

1. Add failing tests for `application/json` with `format: raw`, `application/json` with markdown/text, ordinary HTML POST response, and binary POST response. The raw JSON case must emit the exact response text and its content fingerprint; the other media guard cases must return nonretryable errors without extracted content.
2. Extend the content-type guard for the raw JSON case. Update plugin usage text, assistance, and example configuration to describe POST as a public JSON retrieval option, and document body audit visibility and redirect behavior. Generate the catalog knob golden from the live `CatalogServiceImpl` schema, then review the diff rather than editing the golden by guess.
3. Run `tests/unit/plugins/transforms/test_web_scrape.py`, `tests/unit/plugins/transforms/test_external_catalogue_metadata.py`, `tests/unit/web/catalog/test_service.py`, and `tests/unit/web/catalog/test_knob_schema_golden.py`.

## Task 5 — Whole-tree and integration gates

**Files:** `src/elspeth/plugins/transforms/web_scrape.py`; `docs/architecture/dag/scenario-corpus/v1/manifest.yaml` and dependent pins only if the gates identify this plugin's source hash there.

1. Format and lint touched Python files. Recompute `WebScrapeTransform.source_file_hash` with `scripts.cicd.plugin_hash.compute_source_file_hash` after formatting, then update any scenario-corpus literal hash and dependent projection pins in the order documented in `CONTRIBUTING.md` § plugin inventories. Keep `plugin_version` unchanged.
2. Run affected whole-tree gates: plugin validation parity, transform contract, plugin metadata/catalog, DAG scenario corpus, process-path errors, forward-invariant probes, and the trust-tier lint comparison. For any exact-set gate changed by the implementation, regenerate from the live scanner or registry; prove its positive and negative controls rather than changing an expected count by guess.
3. Because this changes a plugin configuration contract and a shared external-call path, run the canonical frozen `scripts/full-suite-gate.sh --execute --detach` on an isolated, quiescent worktree. Inspect its `summary.txt` and `frozen` result. Run PostgreSQL testcontainers only if implementation actually changes schema, SQL, session/Landscape persistence, or locking.
4. Independently review the diff for value leakage, redirect semantics, request/audit mismatch, GET compatibility, and whether `format: raw` returns JSON verbatim. Then use the repository's branch safety and integration procedure for the confirmed target branch.

## Acceptance examples

| Case | Expected result |
|---|---|
| Existing GET configuration | Same wire method, redirect handling, outputs, and audit request shape |
| POST with valid row JSON | One IP-pinned POST; audited body; extracted HTML/text or raw JSON; same output field contract |
| Missing, invalid, or oversized body | Nonretryable row error before DNS or HTTP; value-free reason |
| POST 3xx | Recorded initial response and nonretryable row error; no redirect hop |
| POST 429, 5xx, timeout after send | Audited failure and nonretryable row error; no automatic reissue |
| Replay / verify body mismatch | Replay cannot consume different evidence; verify refuses before egress |
| `application/json` with markdown/text | Media-type error; no JSON accidentally treated as HTML |

## Scope choices for review

This first version assumes per-row JSON request objects and supports JSON responses only as raw text. Form submissions, fixed configured bodies, authenticated APIs, automatic POST redirect following, and parsed JSON output fields would be separate design choices. If the intended endpoint needs one of those, revise this plan before implementation.
