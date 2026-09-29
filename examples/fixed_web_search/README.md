# Fixed web search

This example keeps a government search destination in the `web_scrape` node
and reads the search value from each CSV row. `settings_fixture.yaml` uses a
local government-style directory with the query key `q`; `settings_abn_page.yaml`
uses ABN Lookup's public results address with the query key `SearchText`. The
plugin code is identical for both.

Run the three three-row local fixtures with
`./examples/fixed_web_search/run_fixture.sh directory` and
`./examples/fixed_web_search/run_fixture.sh registry`, then
`./examples/fixed_web_search/run_fixture.sh form`. The first uses `q` and
HTML list items; the second uses `term` and HTML articles with detail links
and registration text. The form variant sends ordered URL-encoded POST fields,
including a repeated `scope` field and a Unicode search value. All three
assert three HTTP requests and three output rows, including one result,
multiple possible results, and no results. Each replays
its three calls offline and verifies them against the restarted fixture. The
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
