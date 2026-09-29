# Web Scrape Transform

Fetch webpages from URLs, convert content to markdown/text, and generate fingerprints for change detection.
It can also POST a JSON object or URL-encoded form from each row to a read-only data endpoint.

## Configuration

```yaml
transforms:
  - plugin: web_scrape
    options:
      url_field: url
      content_field: page_content
      fingerprint_field: page_fingerprint
      format: markdown  # markdown | text | raw
      text_separator: " "  # text format only
      fingerprint_mode: content  # content | full

      http:
        abuse_contact: compliance@example.com
        scraping_reason: "Compliance monitoring"
        timeout: 30
        allowed_hosts: public_only  # public_only | allow_private | CIDR list

      strip_elements:
        - script
        - style
        - nav
```

Set exactly one URL source: `url_field` names a field containing an absolute
HTTP(S) address in each row; `url` fixes the absolute address in the node.
The latter is useful when many rows search the same site. Both use the same
request-time SSRF validation and IP pinning. A fixed URL is also checked at
config time without DNS resolution. `blob_fetch` supports the same URL choice.

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
      schema: {mode: observed}
```

This ABN Lookup address is an example of a government search result page. The
same options work with another page by changing its URL and query names. Use
`query` for fixed parameters, such as `{page: '1'}`. A row-bound query value
must be a string; missing, invalid, or oversized URLs fail before DNS. Query
parameter names associated with credentials are rejected. Query values and the
resulting URL are audit evidence, so search terms should not contain secrets.

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

POST does not follow redirects or retry failed requests automatically. A
restarted run may still issue the request again, so use this mode only for
endpoints where repeated retrieval is safe. Request JSON is retained in the
HTTP audit trail; do not put credentials in the body.

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
candidate as unverified source data.

## Output Fields

| Field | Type | Description |
|-------|------|-------------|
| `{content_field}` | str | Extracted content |
| `{fingerprint_field}` | str | SHA-256 fingerprint |
| `fetch_status` | int | HTTP status code |
| `fetch_url_final` | str | Final URL after redirects |
| `fetch_url_final_ip` | str | Final resolved IP after redirects |
| `{records.field}` | list | CSS-selected candidate objects, when `records` is configured |

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
- Scheme whitelist (http/https only)
- SSL certificate verification (always enabled)

## Installation

The Web Scrape transform's HTTP and HTML dependencies are part of the base
project dependencies; there is no separate `web` extra. From a source
checkout, install the locked environment with:

```bash
uv sync --frozen
source .venv/bin/activate
```
