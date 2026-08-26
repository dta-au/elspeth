"""The synthetic fixtures under website/tutorial-site/ (Phase p4 onwards).

Reads the SOURCE files under website/tutorial-site/, which is the GitHub Pages
publish tree. Two kinds live there: the 3 scrape pages the guided tutorial
fetches, which must be unmistakably marked test data, noindexed, and carry
three tables whose values DIFFER across the three projects so the derived facts
vary; and multi-doc-sections.json, the corpus the collector-authoring scenario
prompt cites.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_WEBSITE = _ROOT / "website/tutorial-site"
_FRONTEND_PUBLIC = _ROOT / "src/elspeth/web/frontend/public/tutorial-site"
_PAGES = ("project-1.html", "project-2.html", "project-3.html")


@pytest.mark.parametrize("name", _PAGES)
def test_synthetic_page_is_marked_test_data(name: str) -> None:
    html = (_WEBSITE / name).read_text(encoding="utf-8")
    assert "SYNTHETIC TEST DATA ONLY — DO NOT USE" in html
    # Match either the self-closing (' />') or plain ('>') form so the
    # assertion agrees with the XML self-closing fixtures below.
    assert 'content="noindex"' in html


@pytest.mark.parametrize("name", _PAGES)
def test_synthetic_page_has_three_tables(name: str) -> None:
    html = (_WEBSITE / name).read_text(encoding="utf-8").lower()
    # Risk register / schedule / cost breakdown headings.
    assert "risk register" in html
    assert "schedule" in html
    assert "cost breakdown" in html
    # The cost table must be summable (>= 3 explicit dollar figures).
    assert html.count("$") >= 3


@pytest.mark.parametrize("name", _PAGES)
def test_frontend_public_tree_does_not_duplicate_tutorial_page(name: str) -> None:
    assert not (_FRONTEND_PUBLIC / name).exists()


def test_synthetic_pages_have_distinct_cost_totals() -> None:
    # The whole point of differing values: the derived total_cost must vary.
    import re

    totals: list[int] = []
    for name in _PAGES:
        html = (_WEBSITE / name).read_text(encoding="utf-8")
        figures = [int(m.replace(",", "")) for m in re.findall(r"\$([\d,]+)", html)]
        assert figures, f"{name} has no dollar figures"
        totals.append(sum(figures))
    assert len(set(totals)) == 3, f"cost totals must differ across pages, got {totals}"


def test_synthetic_pages_have_distinct_go_live_dates() -> None:
    # The derived key_date must vary: every page's Go-live row carries a
    # different ISO date. Steps 3-4 copy project-1 verbatim and change ONLY the
    # values, so this guards a forgotten date edit at CI (cheap) instead of at
    # the expensive staging judge run.
    import re

    dates: list[str] = []
    for name in _PAGES:
        html = (_WEBSITE / name).read_text(encoding="utf-8")
        m = re.search(r"Go-live</td><td>(\d{4}-\d{2}-\d{2})</td>", html)
        assert m, f"{name} has no Go-live date row"
        dates.append(m.group(1))
    assert len(set(dates)) == 3, f"go-live dates must differ across pages, got {dates}"


def test_synthetic_pages_have_distinct_project_names() -> None:
    # The derived project_name must vary: each hero <h1> names a distinct
    # project. Guards a forgotten title edit (same copy-verbatim risk).
    import re

    names: list[str] = []
    for name in _PAGES:
        html = (_WEBSITE / name).read_text(encoding="utf-8")
        m = re.search(r"<h1>([^<]+)</h1>", html)
        assert m, f"{name} has no hero <h1>"
        names.append(m.group(1).strip())
    assert len(set(names)) == 3, f"project names must differ across pages, got {names}"


# --- multi-doc-sections.json -------------------------------------------------
# The collector-authoring scenario's canonical prompt cites
# {base}/tutorial-site/multi-doc-sections.json. It is published from the same
# tree as the 3 scrape pages, so the URL resolves like they do; the shape below
# is what the corpus-register variant of that prompt describes (document id,
# title, list of sections of text).

_MULTI_DOC = _WEBSITE / "multi-doc-sections.json"
_COLLECTOR_SPEC = _ROOT / "src/elspeth/web/frontend/tests/e2e/tutorial-reliability.staging.spec.ts"


def _load_multi_doc() -> dict:
    import json

    return json.loads(_MULTI_DOC.read_text(encoding="utf-8"))


def test_multi_doc_fixture_is_marked_test_data() -> None:
    assert "SYNTHETIC TEST DATA ONLY — DO NOT USE" in _load_multi_doc()["_notice"]


def test_multi_doc_fixture_documents_carry_the_named_shape() -> None:
    documents = _load_multi_doc()["documents"]
    assert len(documents) >= 3
    for doc in documents:
        assert set(doc) == {"document_id", "title", "sections"}
        assert doc["document_id"] and doc["title"]
        assert isinstance(doc["sections"], list) and doc["sections"]
        assert all(isinstance(s, str) and s.strip() for s in doc["sections"])
    ids = [doc["document_id"] for doc in documents]
    assert len(set(ids)) == len(ids), f"document ids must be unique, got {ids}"


def test_multi_doc_fixture_section_counts_differ() -> None:
    # Same discipline as the pages' differing costs/dates/names: per-document
    # batches must be distinguishable, so a require_all collector that drops a
    # section is observable rather than masked by a uniform count.
    counts = [len(doc["sections"]) for doc in _load_multi_doc()["documents"]]
    assert len(set(counts)) == len(counts), f"section counts must differ, got {counts}"


def test_frontend_public_tree_does_not_duplicate_multi_doc_fixture() -> None:
    assert not (_FRONTEND_PUBLIC / _MULTI_DOC.name).exists()


def test_collector_scenario_prompt_url_resolves_to_a_published_file() -> None:
    # Derived from the prompt itself rather than restated: the scenario's URL
    # is the authority for what must be published, so a renamed fixture (or a
    # renamed prompt target) fails here instead of 404-ing at run time.
    import re

    spec = _COLLECTOR_SPEC.read_text(encoding="utf-8")
    cited = set(re.findall(r"https://dta-au\.github\.io/elspeth/tutorial-site/([\w.-]+)", spec))
    assert cited, "collector scenario spec cites no tutorial-site URL"
    for name in sorted(cited):
        assert (_WEBSITE / name).exists(), f"{name} is cited by the collector scenario but not published"
