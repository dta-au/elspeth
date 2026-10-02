# Web Scrape Transform

Fetch webpages from URLs, convert content to markdown/text, and generate fingerprints for change detection.
It can also POST a JSON object, URL-encoded form, or multipart form from each row to a read-only data endpoint.

## Configuration

```yaml
transforms:
  - plugin: web_scrape
    options:
      url_field: url
      content_field: page_content
      fingerprint_field: page_fingerprint
      format: markdown  # markdown | text | raw
      response_mode: page  # page | json | xml; JSON/XML require format: raw
      charset_policy: declared_or_utf8  # or utf8_only
      accepted_mime_types: []  # optional exact MIME allowlist within the mode
      text_separator: " "  # text format only
      fingerprint_mode: content  # content | full

      http:
        abuse_contact: compliance@example.com
        scraping_reason: "Compliance monitoring"
        timeout: 30
        max_body_bytes: 10485760  # decoded streaming cap
        max_decoded_body_bytes: 10485760  # optional narrower post-fetch cap
        max_encoded_body_bytes: 10485760
        max_decompression_ratio: 200
        allowed_hosts: public_only  # public_only | allow_private | CIDR list
        allowed_origins: [https://example.gov.au]  # optional exact scheme, host, port

      strip_elements:
        - script
        - style
        - nav
```

### URL, origin policy, and query parameters

Set exactly one URL source: `url_field` names a field containing an absolute
HTTP(S) address in each row; `url` fixes the absolute address in the node.
The latter is useful when many rows search the same site. Both use the same
request-time SSRF validation and IP pinning. A fixed URL is also checked at
config time without DNS resolution. `blob_fetch` supports the same URL choice.

Set `http.allowed_origins` to restrict the initial URL and every redirect to
the listed HTTP(S) origins. Matching uses the scheme, hostname, and effective
port (80 or 443 when omitted); subdomains and a switch from HTTPS to HTTP are
different origins. An unapproved row URL is refused before DNS. This setting
does not replace `http.allowed_hosts`: the resolved IP is still checked against
that policy. `blob_fetch` accepts the same origin setting.

For a public search endpoint, keep the destination in `url` and map each
query parameter to a row field with `query_fields`:

```yaml
transforms:
  - plugin: web_scrape
    options:
      url: https://abr.business.gov.au/Search/ResultsActive
      query_fields: {SearchText: search_text}
      content_field: search_results
      fingerprint_field: search_fingerprint
      format: raw
      http:
        abuse_contact: compliance@example.com
        scraping_reason: Approved public register search
        allowed_origins: [https://abr.business.gov.au]
      schema: {mode: observed}
```

This ABN Lookup address is an example of a government search result page. The
same options work with another page by changing its URL and query names. Use
`query` for fixed parameters, such as `{page: '1'}`. A row-bound query value
must be a string; missing, invalid, or oversized URLs fail before DNS. Query
parameter names associated with credentials are rejected. Query values and the
resulting URL are audit evidence, so search terms should not contain secrets.

### Request headers

For public pages that require request headers, use `headers` for fixed values
and `header_fields` to read values from each row:

```yaml
headers: {Accept: text/html}
header_fields: {Accept-Language: preferred_language}
```

The supported names are `Accept`, `Accept-Language`, `User-Agent`, and
`X-Requested-With` (case-insensitive). Names that control transport, body
framing, authentication, cookies, or ELSPETH's scraping identity are rejected
at configuration time. Header values must be nonempty ASCII without control
characters, at most 1,024 bytes each and 4,096 bytes together. Missing or
invalid row values fail before DNS. Configured header values are sent unchanged;
the request audit and telemetry contain only an HMAC fingerprint of each value.
`ELSPETH_FINGERPRINT_KEY` is required for configured headers in every mode,
including when `ELSPETH_ALLOW_RAW_SECRETS=true`, so replay and verify distinguish
different values without exposing them through an unkeyed digest. Do not put
credentials in these headers; authenticated requests will use dedicated
secret-reference options.

When any configured header is present, GET redirects must stay on the
initial request's exact scheme, hostname, and effective port. A cross-origin
redirect is recorded as a blocked hop and is never fetched, even when
`http.allowed_origins` otherwise permits its destination.

### JSON POST requests

GET is the default. To request data with POST, add a JSON object to each input
row and name its field explicitly:

```yaml
transforms:
  - plugin: web_scrape
    options:
      url_field: endpoint_url
      method: POST
      request_json_field: query_body
      content_field: response_text
      fingerprint_field: response_fingerprint
      format: raw
      http:
        abuse_contact: compliance@example.com
        scraping_reason: Public data retrieval
        max_request_body_bytes: 1048576
      schema: {mode: observed}
```

`request_json_field` must name a row field containing a JSON object. Missing,
invalid, or oversized bodies fail that row before DNS resolution or any HTTP
request. The serialized body limit defaults to 1 MiB. `format: raw` returns a
JSON response as text, ready for a downstream JSON transform; `markdown` and
`text` accept HTML and other text responses, not `application/json`.

POST does not follow redirects or retry failed requests automatically.
Pipelines containing POST retrieval refuse automatic resume. Starting a new
run can issue the request again, so use this mode only for endpoints where
repeated retrieval is safe. Request JSON is retained in the HTTP audit trail;
do not put credentials in the body.

### Response syntax, charset, and size limits

The default `response_mode: page` retains the existing HTML/text extraction
behavior. To validate an API response as JSON or XML before it receives a
fingerprint, set `format: raw` and `response_mode: json` or `xml`. JSON must be
valid, complete UTF-8 JSON with no `NaN`, `Infinity`, or duplicate object keys. XML must be well formed
and cannot contain a document type declaration or entity declaration. The
result remains text for a downstream parser; no untrusted document structure
is added to the row by this option.

Response bytes are decoded strictly, using a supported declared charset or
UTF-8 when none is declared. Supported charsets are UTF-8, ASCII, ISO-8859-1,
Windows-1252 and UTF-16 variants. `charset_policy: utf8_only` rejects other
declared charsets. `accepted_mime_types` narrows the response mode to a list of
exact MIME types. Missing, binary, or unaccepted content types fail the row.
The HTTP client accepts identity, gzip and deflate transfer encodings, bounds
encoded and decoded bytes separately, and rejects excessive expansion. Empty,
partial (206), no-content (204/205), and 304 responses do not become page
fingerprints. A 304 cannot be used until conditional content reuse has a
versioned cache and audit contract.

### URL-encoded POST forms

For an HTML form, set `method: POST`, a fixed `url` or a row `url_field`, and
`request_form_field` instead of `request_json_field`:

```yaml
transforms:
  - plugin: web_scrape
    options:
      url: https://example.org/search
      method: POST
      request_form_field: search_form
      content_field: result_html
      fingerprint_field: result_hash
      format: raw
      http:
        abuse_contact: compliance@example.com
        scraping_reason: Approved public search
      schema: {mode: observed}
```

Each row's `search_form` is an ordered list of `{name: string, value: string}`
objects, for example `[{'name': 'q', 'value': 'A B'}, {'name': 'q', 'value':
'Café'}]`. Duplicate names and order are preserved; values are UTF-8 encoded
as `application/x-www-form-urlencoded`. An invalid or oversized form fails
before DNS. The body limit is `http.max_request_body_bytes` (1 MiB by default)
and at most 256 fields are accepted. Form values and a digest of the exact
encoded body are retained in HTTP audit evidence; do not put credentials in
the form row. This mode submits HTTP form data; filling a rendered browser
form is a separate planned browser capability.

### Multipart POST forms

For a multipart search, use `request_multipart_field` instead of the JSON or
URL-encoded form field. The row value is an ordered list of up to 256 parts:

```json
[
  {"name": "q", "value": "Café"},
  {"name": "attachment", "blob_ref": "<sha256 payload-store ref>", "filename": "query.txt", "content_type": "text/plain"},
  {"name": "scope", "value": "public"}
]
```

File parts read only content-addressed payload-store bytes. Local file paths
are not accepted. Parts retain their order and repeated names. The complete
encoded body must fit `http.max_request_body_bytes`; a missing or oversized
blob fails before DNS. The audit request records each part, the content type,
boundary, exact body size, and SHA-256 digest. Replay binds each file reference
to the source run's declared multipart field and verifies the retained bytes.
Do not put credentials in text or file parts.

### HTML record extraction

For an HTML search page, `records` extracts a bounded list of candidate
records into one output field:

```yaml
records:
  field: candidates
  selector: main li.result
  max_records: 100
  columns:
    - {field: name, selector: a, required: true}
    - {field: detail_path, selector: a, attribute: href}
    - {field: status, selector: .status}
```

Each selected element produces one object. A column without a selector reads
the selected element itself; `attribute` reads an HTML attribute instead of
text. Missing optional values become `null`; missing required values fail the
row. Invalid selectors fail configuration validation. More than `max_records`
matches fails the row instead of silently dropping candidates. The default
limits are 200 records, 4,096 characters per value, and 100,000 characters
across all extracted values. This only accepts HTML responses. Configure
selectors for each site after inspecting its current markup, and treat every
candidate as unverified source data. Set `records.provenance_field` to retain
aligned source URL, selector, attribute, and match evidence for each column.
An HTML `href` column with `resolve_url: true` resolves relative links against
the final page URL and requires explicit `http.allowed_origins`.

### JSON record extraction

For a JSON API, set `format: raw`, `response_mode: json`, and select records
with exact object keys and array indexes:

```yaml
records:
  field: candidates
  provenance_field: candidate_sources
  records_path: [results]
  columns:
    - {field: name, path: [name], required: true}
    - {field: status, path: [registration, status]}
```

`records_path` must select an array of objects. Column paths select scalar
values or scalar arrays; `multiple: first`, `one`, or `all` controls the
selection. JSON extraction bounds document size, record count, values per
record, total values, individual value length, and the combined output and
provenance size. JSON and CSS record configurations cannot be mixed. Extracted
values remain untrusted.

### GET pagination

For bounded GET pagination, configure exact allowed origins and either a
CSS next link or an HTTP `Link` header:

```yaml
pagination:
  mode: next_link_css
  next_link_selector: a.next
  max_pages: 5
  max_elapsed_seconds: 120
  max_total_body_bytes: 10485760
  max_total_content_chars: 1000000
  max_total_records: 1000
```

For JSON APIs, use `mode: link_header` and omit `next_link_selector`. Each
page passes the same SSRF, origin, response syntax, MIME, charset, and byte
limits, and is individually audited. All pages enrich one input row; extracted
records and their provenance are combined in order. Page-mode raw text is
joined with newlines. Paginated JSON emits an array of the admitted page
documents. Paginated XML emits a `pages` element with one `page` child per
response; each child's text preserves the original XML document. These
envelopes also apply to a single page, and their size counts against the
aggregate content limit. Audit metadata records every page and the stop reason.
Repeated URLs and `max_pages` stop before another fetch; elapsed or aggregate
limits fail the row. POST pagination and opaque credential or cursor links
are refused.

### Unavailable authenticated and browser execution

Authenticated execution remains unavailable: configured `auth` is hidden
from Composer's catalog and refuses the row before DNS or HTTP. Response
validation does not protect confidential authenticated bodies, which the
ordinary audit client persists before extraction. Enabling auth requires the
retention and access policy and protected evidence boundary described in the
[authenticated response design gate](../design/2026-09-29-web-auth-response-evidence-gate.md).

Live browser execution is also unavailable. The
[`core/browser` boundary](../../src/elspeth/core/browser/boundary.py) supplies
bounded request-admission and archived-output replay contracts; it opens no
sockets, launches no browser, and registers no pipeline plugin. The
[browser egress release gate](../design/2026-09-29-web-browser-egress-gate.md)
requires a separately isolated worker and enforcing gateway, with deployment
proof that browser traffic cannot bypass audit and destination policy.

## Output Fields

| Field | Type | Description |
|-------|------|-------------|
| `{content_field}` | str | Extracted content |
| `{fingerprint_field}` | str | SHA-256 fingerprint |
| `fetch_status` | int | HTTP status code |
| `fetch_url_final` | str | Final URL after redirects |
| `fetch_url_final_ip` | str | Final resolved IP after redirects |
| `{records.field}` | list | CSS-selected or JSON-path candidate objects, when configured |
| `{records.provenance_field}` | list | Aligned column source evidence, when configured |

## Text Extraction And Line Splitting

`format: text` defaults to `text_separator: " "`, which returns compact plain
text — a single logical line.

When a downstream `line_explode` transform consumes `web_scrape` output, the
composer's semantic validator emits a `semantic_contracts` violation with
`requirement_code: line_explode.source_field.line_framed_text`. Composer agents
should call

```text
get_plugin_assistance(
    plugin_name="line_explode",
    issue_code="line_explode.source_field.line_framed_text",
)
```

to retrieve the current structured fix guidance — including before/after
configuration examples — directly from the plugin. This document no longer
hardcodes the fix; the plugin owns it.

## Security

- SSRF prevention (blocks private IPs, loopback, cloud metadata)
- Configurable host policy via `http.allowed_hosts`
- Optional exact destination and redirect policy via `http.allowed_origins`
- Scheme whitelist (http/https only)
- SSL certificate verification (always enabled)
- `Link` response headers are retained for replay only when their full value
  contains simple links and bounded noncredential query values, such as a
  search term and page number. Complex links, credential-shaped parameters,
  and opaque cursor/capability parameters are redacted in the audit record and
  cannot be replayed as exact transport evidence.

## Installation

The Web Scrape transform's HTTP and HTML dependencies are part of the base
project dependencies; there is no separate `web` extra. From a source
checkout, install the locked environment with:

```bash
uv sync --frozen
source .venv/bin/activate
```
