# Modern web fetch capability plan

**Status:** In progress. The POST-first HTTP, exact-origin, and multipart slices are locally merged on `release/0.8.1` through `4093249d8` (29 September 2026): fixed or row URLs, row-mapped query values, ordered URL-encoded and multipart POST forms with bounded payload-store file parts, bounded CSS candidate extraction, two distinct local search-page layouts, initial and redirect origin checks, and live/replay/verify fixture runs. A hermetic 10,000-row completed GET batch passed; a 30-row batch and a three-row multipart live/replay/verify run also passed from merged release tips. Header/auth policy, response handling, pagination, browser forms and their egress gate, crawl, mid-source restart, and an operator-authorized live public-page canary remain open. The web services have not been restarted with this code because host authentication is required.
**Target:** ELSPETH `release/0.8.1` baseline at `4b83b568` (29 September 2026).
**Goal:** Fetch and extract modern web data, submit HTTP forms, and fill browser forms while preserving ELSPETH's audit, replay, verification, network, and Composer contracts. Government search pages are the motivating class of sites; ABN Lookup is one illustrative page, not the special use case. Node-owned addresses and row-owned search values must work across sites without ABN-specific runtime code.

## Current contract and design choice

The existing [`web_scrape`](../../src/elspeth/plugins/transforms/web_scrape.py) transform accepts **row URLs only** through `url_field`, performs SSRF-pinned GET or bounded JSON POST, extracts markdown/text/raw content, and emits a fingerprint and fetch metadata. JSON POST is already implemented: `request_json_field`, a 1 MiB default request cap, no POST redirects, and no automatic POST retry. [`blob_fetch`](../../src/elspeth/plugins/transforms/blob_fetch.py) preserves bounded binary responses in the payload store. [`AuditedHTTPClient`](../../src/elspeth/plugins/infrastructure/clients/http.py) records HTTP calls and supports replay/verify; its current request DTO has JSON and query fields but no form or multipart body shape. The Composer already applies a private-network gate to both fetch transforms.

**Decision:** Keep `web_scrape` as the row-oriented HTTP page/data transform and `blob_fetch` as the binary path. Add form and request options to their shared HTTP infrastructure rather than a duplicate HTTP client. Add a separate `web_browser` transform for rendered pages and DOM interaction. Add a bounded `web_crawl` source only after the single-URL transforms and shared policy are sound. Each is a normal YAML and Composer plugin with the same runtime path; the browser does not create a special Composer or tutorial path. Existing `web_scrape` GET and JSON POST configurations retain their current meaning.

The alternative of putting a browser inside `web_scrape` would make a simple static fetch silently acquire a browser runtime, wider egress surface, and different replay semantics. The alternative of a generic arbitrary HTTP request plugin would permit mutations without a clear capability policy. This plan instead exposes read-oriented methods first and puts any remote mutation behind a separate, explicit policy decision.

## Government search page batch workflow

The reusable contract is a node-owned fixed `url` or a row-owned `url_field`, plus explicit mappings from request query/form controls to row fields. A pipeline can submit a bounded batch to one government search page without constructing URLs in upstream data. It captures each page or result and its audit references, then applies site-specific selectors and decision rules declared in pipeline configuration. It must never silently treat the first result as a confirmed identity.

[ABN Lookup](https://abr.business.gov.au/) illustrates a public search page that accepts ABN, ACN or name values without login. Its [search results](https://abr.business.gov.au/Search/ResultsActive?SearchText=Australia) also demonstrate an ordinary GET query route. A public-page canary can use it after site-appropriate contact and rate settings are supplied. Its registered [web service](https://abr.business.gov.au/Tools/WebServices) is a separate optional integration that requires an operator-held GUID. No ABN-specific transform is required for the generic web capability.

1. Input rows carry a stable reference and search value. Node configuration declares the destination, query/form field mapping, result selectors, maximum records, and the accepted origin. Search values remain input data and are retained with the resulting evidence.
2. Search outcomes distinguish zero results, one plausible result, multiple candidates, HTTP failure, and extraction failure. Site-specific validation, such as an [ABN checksum](https://abr.business.gov.au/Help/AbnFormat), belongs in configured upstream validation or a separate domain transform when needed. A generic search result is evidence, not an identity or viability verdict.
3. A hermetic 10,000-row search fixture proves one terminal outcome per input, bounded requests and memory, stable provenance, and restart behavior. A small operator-authorized live canary checks the current page contract without load testing a public service.
4. Government-style examples use a node URL and row search text, include ambiguous results and a details page, and extract declared status, name, and location fields. The same code must run distinct page layouts by changing configuration only. ABN Lookup is an optional live canary when approved. Where a site needs browser interaction, the browser security gate below applies.

## Feature inventory and disposition

| Area | Current baseline | Planned contract |
| --- | --- | --- |
| Static HTTP | GET, JSON POST, SSRF-pinned redirects for GET, response cap | Query parameters, validated headers, conditional requests, bounded text/JSON/XML response handling, explicit status policy |
| URL source | Required `url_field` on `web_scrape` and `blob_fetch` | Exactly one of fixed `url` in node options or `url_field` in each row; same choice for `web_browser` |
| HTTP forms | None | `application/x-www-form-urlencoded` and `multipart/form-data`, row-bound fields and payload-store file parts, deterministic body encoding and request evidence |
| Browser forms | None | Render, locate, fill, select, check, upload, submit, wait, and extract using a closed action vocabulary |
| Authentication | No origin-auth option on `web_scrape`/`blob_fetch` | Secret references for Basic/Bearer/API-key; bounded cookie session within one work item; optional approved OAuth client-credentials flow |
| Extraction | Whole-page markdown/text/raw; fingerprint | Structured CSS extraction, links/tables, JSON selectors, file handoff, per-field provenance; optional XPath only if CSS cannot cover a demonstrated case |
| Discovery | One URL per input row | Explicit pagination modes, link discovery and bounded crawl source, dedupe and resume checkpoints |
| Named batch case | No reusable search page example | Distinct government-style fixtures through the same config contract, with ABN public-page configuration as an exemplar |
| Operations | Timeout, response/request size, shared rate limiter | Per-origin concurrency and pacing, `Retry-After`, budgets, robots policy, cache validators, observability and cancellation |
| Outputs | Content, fingerprint, status, final URL/IP | Normalized response metadata, selected headers, content type/charset, raw and processed hashes, page/action evidence, structured records |

### 1. HTTP request and response contract

- Make `url` and `url_field` mutually exclusive, with exactly one required on `web_scrape`, `blob_fetch`, and `web_browser`. `url` is an absolute static node option validated during config/preflight; `url_field` is a declared input field resolved and validated separately for every row. Neither accepts implicit URL concatenation or a template with row substitutions. Query/form/body fields provide row-varying search terms while the destination remains fixed. The resolved URL is included in each call's audit evidence and output provenance in either mode. Update input-schema declaration, collision checks, forward-invariant probes, plugin assistance, Composer catalog/schema and preflight to cover both branches. Existing `url_field` configurations remain valid.
- Extend `WebScrapeConfig` with an **exclusive** body selector: current `request_json_field`, `request_form_field`, or `request_multipart_field`. GET has no body; POST has exactly one declared body selector. Form values are validated scalar or ordered repeated values; multipart parts are text or references to bounded payload-store blobs, never arbitrary local filesystem paths. Preserve order and duplicate field names because forms can depend on both.
- Add `query` and an allowlisted `headers` mapping with static or row-field values. Reject hop-by-hop and transport-controlled headers (`Host`, `Content-Length`, `Transfer-Encoding`, `Connection`, `Cookie`, `Authorization`) from general overrides; auth and cookies use dedicated fields. Validate header bytes, size, and field names before DNS. Keep audit-safe URL/header fingerprinting and prevent values from being copied into telemetry or user-facing errors.
- Add response modes `html`, `text`, `json`, `xml`, and `binary_ref` only where the output schema can describe them. Retain existing `format: markdown|text|raw` behavior as the default page output. `json` and `xml` are strict, bounded parses; malformed data produces a row error. `binary_ref` delegates storage and metadata behavior to the `blob_fetch` owner rather than embedding bytes in rows. Support explicit accepted MIME types, charset policy, content-encoding limits, decompression-ratio cap, and a separate decoded-size cap.
- Expose status, logical final URL, final IP, content type, selected safe response headers, ETag/Last-Modified, byte count, raw hash, processed hash, and whether a 304 reused archived content. Fingerprint semantics and conditional fetch behavior must be specified before caching is enabled; a 304 cannot be fingerprinted as an empty page.
- GET/HEAD may use bounded retries with documented status/network rules; POST remains nonretrying by default. Allow an explicit idempotency policy for read-only POST endpoints only after resume and verify behavior is proven. Keep redirects off for bodies by default; any opt-in redirect policy must define 301/302/303/307/308 method/body semantics, credential stripping across origins, and per-hop SSRF checks.
- Defer PUT/PATCH/DELETE and arbitrary bodies to a separately reviewed remote-mutation capability. They are relevant HTTP features, but a data-fetch transform must not turn an untrusted row into an unrestricted write operation.

**Illustrative HTTP form configuration** (field names are proposed and may be refined with the config tests):

```yaml
transforms:
  - plugin: web_scrape
    options:
      url: https://example.org/search
      method: POST
      request_form_field: form_fields
      content_field: result_html
      fingerprint_field: result_hash
      format: raw
      http:
        abuse_contact: operator@example.org
        scraping_reason: Approved public search
        allowed_hosts: public_only
        max_request_body_bytes: 1048576
```

The input `form_fields` is an ordered list such as `[{name: query, value: glaciers}, {name: page, value: '1'}]`. Repeated names remain repeated. The request's exact encoded bytes and content type are audited under a versioned body representation; credentials are resolved from secret references and are never embedded in that row.

### 2. Browser rendering and form filling

`web_browser` is a separate transform with one isolated browser context per row by default. It accepts exactly one starting URL option (`url` or `url_field`), a fixed ordered action list, and output selectors. Actions are a closed set: `navigate`, `wait_for`, `fill`, `select`, `check`, `uncheck`, `click`, `upload`, `submit`, `extract`, `download`, and `screenshot`. Each action has a typed selector, a timeout, a bounded expected outcome, and a stable action index. No arbitrary JavaScript, shell command, or row-supplied Playwright script is accepted. Default locator priority is accessible role/label, then test ID, then CSS; selectors must be unique or declare an index explicitly. `fill` values may come from row fields or approved secret references; file uploads come only from payload-store blobs with name, MIME, and size controls.

**Illustrative browser flow:**

```yaml
transforms:
  - plugin: web_browser
    options:
      url: https://abr.business.gov.au/
      actions:
        - {action: fill, by: label, selector: Search, value_field: company_query}
        - {action: click, by: role, selector: 'button:Search', expect: navigation}
        - {action: wait_for, by: role, selector: main, state: visible}
      output:
        format: markdown
        selector: main
      limits:
        max_actions: 20
        max_navigation_count: 5
        max_wall_seconds: 90
```

The example shows the desired node/row split, not verified ABN Lookup locators; the implementation must derive locators from a live inspected page and pin them in the canary. The flow supports JavaScript-rendered pages, hidden/CSRF fields handled by the browser, checked controls, file inputs, downloads, and a bounded login sequence. The browser context retains cookies only within one row/work item unless a separately reviewed session-scope feature is added. Default headless Chromium is an optional, pinned runtime dependency, not silently installed with base HTTP fetching. Capture the rendered DOM snapshot, selected output, navigation/action outcomes, downloads, and optional screenshot as bounded payload-store artifacts. A screenshot is evidence, not extracted text.

**Browser release gate:** Playwright request routing alone is insufficient as the security boundary. The browser worker must run with direct egress denied and all DNS, HTTP(S), WebSocket, worker, iframe, popup, redirect, and download traffic forced through an enforcing gateway or equivalent network sandbox. The gateway must apply host policy, DNS validation/IP pinning, origin/port rules, redirect checks, byte/time/request caps, and per-request audit before forwarding. Block service workers initially so requests cannot bypass route observation; prove this with a hostile local fixture. If complete interception or replay evidence cannot be demonstrated, release only HTTP forms and keep `web_browser` unavailable in the catalog.

### 3. Authentication, sessions, and remote actions

- Allow Basic, Bearer, and API-key authentication via secret references with explicit origin and HTTPS binding. Add OAuth client credentials only when token acquisition, expiry, and refresh can be audited without persisting secrets; interactive SSO/MFA is an operator-assisted flow, not automated credential capture.
- Never accept credentials from arbitrary row text or literal YAML options. Redact authorization headers, cookies, hidden form secrets, and screenshots/DOM fragments containing secrets in operator-visible output. The private evidence store may hold encrypted request material only under a documented retention and replay policy; verify mode must compare secret identity/shape without exposing values.
- Browser login and form submission are potentially state-changing. Classify actions as read-only retrieval versus remote mutation before dispatch. For search and login, require an allowlisted origin and an explicit operator-supplied purpose; for write forms, uploads, purchases, account changes, and DELETE/PUT/PATCH, require a distinct mutation capability and review of retry/resume policy. Never infer safe retry from HTTP method alone.
- Cookies remain origin-scoped and isolated per row. No cross-row cookie jar, browser profile reuse, or third-party tracking storage by default. Allowlist any cross-origin auth redirect explicitly; strip credentials on unexpected origin transitions.

### 4. Discovery, extraction, and freshness

- Add structured extraction as a transform output schema: named fields from CSS selectors/attributes or JSON paths, required/optional fields, multi-value rules, whitespace normalization, and the source URL/selector for each field. Keep extraction output as untrusted content for downstream prompt-shield and Composer checks. Add link and table extraction with a declared maximum record count; do not execute page-supplied scripts to transform data.
- Add pagination modes: explicit next-link selector, `Link` header, bounded URL template, or cursor field from JSON. Each subsequent URL is revalidated against host/network policy. Record page number, cursor/next-link provenance, stop reason, and duplicate detection. Multiple records per page require a clear row-expansion contract and output-schema validation.
- Add `web_crawl` as a source only after pagination and policy are stable. Seed URLs, allowed origins, maximum depth/pages/bytes/wall time, canonicalization, dedupe key, scheduling order, checkpoint/resume, and stop reasons are explicit. Prevent unbounded spidering and query-string explosion. Respect a configurable robots policy with a conservative default for public crawling; record the fetched robots rules and decision. Robots rules are a courtesy protocol, not authorization.
- Support ETag/Last-Modified conditional GET and an opt-in cache with origin/credential partitioning, TTL, response/body hash, and auditable reuse. Never reuse a cached authenticated response across principals. Include HTTP validators and cache hit/miss in the request/output evidence. No hidden stale fallback.

### 5. Resource, network, and audit controls

- Preserve current SSRF IP pinning and per-hop validation in the HTTP path. Add domain/origin allowlists on top of `allowed_hosts`; `public_only` alone does not define where a pipeline is authorized to send form values. Apply the same policy to every browser subrequest. Explicitly reject non-HTTP(S) schemes, local files, data URLs, browser extension URLs, and credential-bearing URLs.
- Bound request count, response bytes, decoded bytes, form/body bytes, redirects, browser actions, navigation count, pages, records, total wall time, and per-origin concurrency. Use a shared per-origin pacing budget across workers, honor `Retry-After` within the run budget, and expose stop reasons without leaking URLs with secrets. Define cancellation cleanup for HTTP streams, browser contexts, downloads, and audit state.
- Every outbound call records method, canonical logical URL, destination IP, allowed-origin decision, request body encoding/hash and safe evidence reference, response status/headers/hash/size, redirect chain, timing, retry cause, and rate-limit decision. For browser runs also record action order, selector contract, navigation/subrequest graph, DOM/output artifact hashes, browser/runtime version, viewport/locale/timezone, and nondeterministic inputs. Avoid putting untrusted response text or secrets into logs or telemetry.
- Replay must consume archived responses and browser artifacts without network access; verify must match an admitted source call/action and fail closed on missing/mismatched evidence. Define an honest fidelity level for browser replay: archived-output replay is supported first; deterministic re-execution of JavaScript is claimed only if an offline browser harness can reproduce it. Never label a live second fetch as replay.
- Route fetched text and structured fields through existing untrusted-content semantics. Composer must validate the same plugin configs as YAML, expose credential-safe guidance, and preserve its provider-authored graph invariant. Browser screenshots and downloads use the existing payload/audit artifact model with explicit content type and retention.

## Delivery sequence and acceptance

Each stage is an independently usable slice; later slices must not silently relax earlier policy. Production plugin changes use an isolated worktree and a frozen integration gate before a release merge.

| Stage | Code and contract work | Acceptance evidence |
| --- | --- | --- |
| 0. Contract and threat boundary | Specify versioned request-body/audit DTO in `src/elspeth/contracts/call_data.py`; map current `web_scrape`, `blob_fetch`, Composer preflight, secret-ref and payload-store ownership. Decide allowed origin and remote-mutation policy. Specify exclusive fixed `url`/row `url_field` schema. | Design review includes exact wire examples, replay/verify cases, all browser egress channels, fixed/row URL validation, and migration behavior for current GET/JSON POST hashes. |
| 1. Fixed URLs and HTTP forms | Add exclusive `url`/`url_field` selection to `web_scrape` and `blob_fetch`; extend `src/elspeth/plugins/infrastructure/clients/http.py` and `src/elspeth/plugins/transforms/web_scrape.py` with ordered URL-encoded forms, then multipart payload-store parts. Update fingerprinting, request/response DTOs, metadata, docs and examples. | Local fixture proves fixed destination plus row-varying query, exact wire bytes, duplicate fields, unicode, 413/429/5xx, body/decoded caps, no POST redirect/retry, blocked SSRF before dispatch, audit refs, offline replay and verify. Existing `url_field` GET/JSON POST cases remain green. |
| 2. Request policy and auth | Add origin allowlist, safe query/header templates, secret-ref auth, isolated cookies, status/charset/MIME handling, strict bounded XML parsing, and conditional GET. Extend Composer preflight in `src/elspeth/web/execution/_validation_authoring.py`. | Credentials cannot reach an unapproved origin or user-visible audit/telemetry; cross-origin redirect and DNS rebinding fixtures fail closed; cache and 304 cases preserve content identity and provenance; XML entities and oversized documents fail safely. |
| 3. Government search batch vertical slice | Build a documented CSV/JSON-to-search-to-results pipeline using only generic HTTP request, extraction, and audit owners. Include distinct government-style fixtures with different query names and result structures, and an illustrative ABN public-page configuration. | 10,000-row hermetic batch, restart, ambiguity, malformed result, HTTP errors, replay/verify, bounded requests, and an operator-authorized small live canary where available. Site-specific identity rules remain explicit pipeline logic. |
| 4. Structured extraction and pagination | Extend extraction owners under `src/elspeth/plugins/transforms/`; add typed output/schema and bounded pagination. Update plugin assistance, catalog schema and semantic checks. | Fixture pages cover missing/multiple selectors, links, tables, JSON, stop conditions, cycles, per-page audit, schema validation and untrusted-content propagation. |
| 5. Browser infrastructure | Add optional pinned browser package, isolated worker/gateway, network sandbox and auditable browser call/action contracts; implement `web_browser` navigation/render only. | Hostile fixture probes direct egress, iframe, popup, WebSocket, service worker, redirects, downloads, DNS rebinding, and offline replay. Browser stays catalog-disabled until all pass. |
| 6. Browser forms | Add exclusive fixed/row URL selection, typed actions, form fill/select/check/upload/submit, waits, login/session handling, output/download/screenshot artifacts. | Fixture proves DOM action order, CSRF form, JS/XHR rendering, selector ambiguity, timeouts, cross-origin auth denial, secret redaction, per-row context isolation, cancellation, replay and verify. Run bounded batches through distinct government-style fixtures with fixed node URLs and row-sourced search terms; ABN Lookup may be one live canary. |
| 7. Crawl source and operations | Add bounded `web_crawl` source, robots parser/cache, per-origin scheduler and checkpoint/resume. | Fixture proves robots allow/disallow/error handling, budgets, dedupe, deterministic scheduling and resume without duplicate unsafe submission. |

For each stage touching plugin code, update the discovery inventory, external-tag guidance, rejection/invariant cases, catalog knob-schema golden, source hash and scenario corpus, lifecycle matrix, docs, and any scanner-owned census named in [`CONTRIBUTING.md`](../../CONTRIBUTING.md#whole-tree-gates-and-conventions-you-will-hit). Run affected unit, property, integration, Composer and replay/verify tests with explicit exit codes; run `ruff`, `mypy`, contracts and keyless lints. Because stages 1–7 change shared runtime or plugin registration, run the canonical `scripts/full-suite-gate.sh --execute --detach` on a frozen tree before merge. If a stage changes persistence/schema or locks, include serial PostgreSQL testcontainer tests. Keyless trust-tier results are diagnostic; operator signing remains a separate post-merge clearance.

## Open decisions before implementation

1. Which origins and remote actions are authorized for the first production use case? Default to public, read-oriented retrieval with no arbitrary mutation.
2. Which origin-auth modes are required immediately? Basic/Bearer/API-key can be staged before OAuth and interactive SSO.
3. Is an optional browser worker with enforced egress available in each target deployment? If not, ship HTTP forms and extraction first and leave the browser capability unavailable.
4. What retention and encryption policy should cover archived form bodies, cookies, DOM snapshots, screenshots, and downloaded files? Resolve this before credentials or browser capture ship.
5. Does crawl belong in the first release? The single-URL and pagination contracts are useful without it; a crawler adds scheduling and robots obligations.

## References

- Existing behavior: [`web_scrape` reference](../reference/web-scrape-transform.md), [`AuditedHTTPClient`](../../src/elspeth/plugins/infrastructure/clients/http.py), [`HTTPCallRequest`](../../src/elspeth/contracts/call_data.py).
- [Playwright form actions](https://playwright.dev/python/docs/input) and [network routing/service-worker caveats](https://playwright.dev/python/docs/network) inform the proposed browser action and egress tests; neither is itself an enforcement boundary.
- [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309.html) defines the robots exclusion protocol; [OWASP SSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html) informs URL, DNS, and redirect defenses.
