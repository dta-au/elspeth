# Fixed web search

This example keeps a government search destination in the `web_scrape` node
and reads the search value from each CSV row. `settings_fixture.yaml` uses a
local government-style directory with the query key `q`; `settings_abn_page.yaml`
uses ABN Lookup's public results address with the query key `SearchText`. The
plugin code is identical for both.

`http.allowed_origins` restricts requests and redirects to exact scheme,
hostname, and port. It is independent of `allowed_hosts`, which still validates
the resolved IP address. Set the origins to the site you are approved to query;
the ABN address is only an example.

Run the local fixtures with
`./examples/fixed_web_search/run_fixture.sh directory` and
`./examples/fixed_web_search/run_fixture.sh registry`, then
`./examples/fixed_web_search/run_fixture.sh form` and
`./examples/fixed_web_search/run_fixture.sh multipart`. The first uses `q` and
HTML list items; the second uses `term` and HTML articles with detail links
and registration text. The form variant sends ordered URL-encoded POST fields,
including a repeated `scope` field and a Unicode search value. The multipart
variant submits ordered text and a content-addressed file part. It also has a
fourth malformed input row that is routed to the error sink, both live and in
replay and verify. Each variant asserts three HTTP requests and three output
rows, including one result, multiple possible results, and no results. Each
replays its three calls offline and verifies them against the restarted fixture. The
page response, bounded `candidates` list, and audit refs are retained. The
pipeline does not claim that any match validates an entity. The runner refuses
to overwrite earlier output or audit files.

Run a 10,000-row hermetic batch with
`./.venv/bin/python examples/fixed_web_search/run_batch.py --count 10000`.
The runner writes to a fresh temporary directory, sets a high rate limit only
for its loopback page, and checks exact input IDs, output rows, HTTP calls,
completed node states, and the zero/one/multiple candidate counts. It prints
the temporary directory, elapsed time, and peak child memory. This rate
setting must not be copied into a public-site configuration.

This batch checks a completed run. ELSPETH currently cannot resume a CSV
source stopped before exhaustion; see the source-resume limitation in
[`checkpoint_resume`](../checkpoint_resume/README.md).

For a small live canary, replace the example contact address with your own,
review the three search terms, then run:

```bash
elspeth run --settings examples/fixed_web_search/settings_abn_page.yaml --execute
```

The live page may change its address or markup. This configuration fetches the
HTML result page; it does not follow detail links or interpret ABN status. A
batch validation workflow needs site-specific selectors and matching rules,
rate limits, and restart handling before using the results as decisions.

## Search, expand candidates, and inspect detail pages

`settings_fixture_detail.yaml` is a generic public-register example. It sends
each input name to a fixed search address, extracts bounded candidate names and
absolute detail URLs, and computes the candidate count. A zero count goes to
`no_results.jsonl`. Other searches expand one row per candidate, fetch each
detail URL under an exact origin allowlist, and extract the legal name,
registry ID, and record status. A one-candidate search goes to
`one_candidate.jsonl`; multiple candidates go to
`ambiguous_candidates.jsonl`, with **all** candidate details retained. The
pipeline does not decide which ambiguous record matches the input, and neither
a detail status nor a one-candidate result is an identity-validation decision.

Run `./examples/fixed_web_search/run_detail_fixture.sh` for a fully local
acceptance check. It uses its own server on port 8215 and refuses to overwrite
existing `runs_detail/` or `output_detail/`. The runner checks three search
requests, three detail requests, the three output classes, and the token-parent
chain from each output back to one of the three source rows. It then replays
all six calls without a server and verifies all six against the restarted
fixture. The three example names and IDs are fictitious. Its search page emits
relative detail links; `resolve_url: true` joins them to the actual final
response URL and admits them under the declared origin policy before the row
is persisted. Resolved candidate URLs and URL/error metadata are checked for
sensitive query values; the fetched HTML remains exact audited response bytes
and may contain site tokens. Sites with secret-bearing HTML need a separate
classified response-evidence boundary. For a real site,
replace the selectors, allowed origin, query key, source, contact address, and
rate policy with the site's actual approved contract; keep the ambiguous and
no-result branches as explicit review outcomes.
