# Tutorial synthetic fixtures

Self-contained synthetic files published to the project's public GitHub Pages
site (`.github/workflows/pages.yaml` serves this tree at the site root, so
`/tutorial-site/<name>` resolves). All of it is invented data, marked as such.

- `project-1.html`, `project-2.html`, `project-3.html` — three "government
  project brief" pages used by the first-run guided tutorial's `web_scrape`
  demo. The tutorial fetches them at `{base}/tutorial-site/project-N.html`
  (`src/elspeth/web/composer/tutorial_sample.py`) and has an LLM write a short
  summary of each. Their values differ deliberately, so the derived facts vary.
- `multi-doc-sections.json` — the multi-document corpus cited by the
  collector-authoring scenario prompt (`COLLECTOR_SCENARIO_PROMPT` in
  `src/elspeth/web/frontend/tests/e2e/tutorial-reliability.staging.spec.ts` and
  `evals/composer-battery/calibration/run_collector_calibration.py`). Three
  documents with `document_id` / `title` / `sections`, each with a different
  section count so a `require_all` collector losing a section is observable.

The base is `https://dta-au.github.io/elspeth` by default; a fork republishing
its own copy overrides it with `ELSPETH_WEB__TUTORIAL_SAMPLE_BASE_URL` (see the
top-level README, "Web Composer" notes).

These files are NOT copied into `src/elspeth/web/frontend/public/tutorial-site/`
— the tutorial deliberately fetches a public, operator-controlled origin rather
than the app's own, so it works on a pure loopback dev box and keeps the
prompt-injection-shield teaching moment deterministic.
`tests/integration/web/test_tutorial_site_pages.py` pins both the contents here
and the absence of the app-served duplicates.
