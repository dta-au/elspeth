# Fixed web search

This example keeps a government search destination in the `web_scrape` node
and reads the search value from each CSV row. `settings_fixture.yaml` uses a
local government-style directory with the query key `q`; `settings_abn_page.yaml`
uses ABN Lookup's public results address with the query key `SearchText`. The
plugin code is identical for both.

Run the three-row local fixture with `./examples/fixed_web_search/run_fixture.sh`.
It asserts three HTTP requests and three output rows, including one result,
multiple possible results, and no results. It then replays all three calls
offline and verifies all three against the restarted fixture. The page
response, bounded `candidates` list, and audit refs are retained. The
pipeline does not claim that any match validates an entity. The runner refuses
to overwrite earlier output or audit files.

For a small live canary, replace the example contact address with your own,
review the three search terms, then run:

```bash
elspeth run --settings examples/fixed_web_search/settings_abn_page.yaml --execute
```

The live page may change its address or markup. This configuration fetches the
HTML result page; it does not follow detail links or interpret ABN status. A
batch validation workflow needs site-specific selectors and matching rules,
rate limits, and restart handling before using the results as decisions.
