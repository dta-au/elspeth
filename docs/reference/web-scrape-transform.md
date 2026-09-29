# Web Scrape Transform

Fetch webpages from URLs, convert content to markdown/text, and generate fingerprints for change detection.
It can also POST a JSON object from each row to a read-only data endpoint.

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

## Output Fields

| Field | Type | Description |
|-------|------|-------------|
| `{content_field}` | str | Extracted content |
| `{fingerprint_field}` | str | SHA-256 fingerprint |
| `fetch_status` | int | HTTP status code |
| `fetch_url_final` | str | Final URL after redirects |
| `fetch_url_final_ip` | str | Final resolved IP after redirects |

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
