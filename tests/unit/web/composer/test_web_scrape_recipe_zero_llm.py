"""Zero-LLM compose gate for the web-scrape recipe (D11, §4.1).

Building the canonical recipe is a pure, deterministic function: it must make
ZERO LLM provider calls. Pins the §4.1 claim that the canonical pipeline
composes with no frontier round-trip at recipe-build time.

P4 owns the recipe build, not the dispatch wiring (P2). These tests prove both
the generic builder and the public legacy-recipe adapter are provider-free:
building the set_pipeline args calls the LLM zero times. The llm node IS
present in the COMPOSED pipeline (it runs at RUN time, never at compose time).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

from elspeth.web.composer.recipes import _build_web_scrape_project_recipe, apply_recipe

_SLOTS = {
    "source_blob_id": str(uuid4()),
    "source_plugin": "json",
    "profile": "tutorial-default",
    "rating_template": "Rate this page from 1-10:\n\n{{ row['content'] }}",
    "abuse_contact": "web-scrape-contact@dta.gov.au",
    "scraping_reason": "Tutorial exercise: fetch public pages for rating",
    "output_path": "outputs/ratings.jsonl",
    # Empty tuple = the slot's declared default: the http.allowed_hosts key is
    # omitted so the web_scrape field default 'public_only' applies.
    "allowed_hosts": (),
}

_GENERIC_SLOTS = {
    "source_blob_id": _SLOTS["source_blob_id"],
    "source_plugin": _SLOTS["source_plugin"],
    "locator_field": "url",
    "profile": _SLOTS["profile"],
    "prompt_template": _SLOTS["rating_template"],
    "response_field": "rating",
    "required_input_fields": ("content",),
    "retained_fields": ("url", "rating"),
    "abuse_contact": _SLOTS["abuse_contact"],
    "scraping_reason": _SLOTS["scraping_reason"],
    "output_path": _SLOTS["output_path"],
    "allowed_hosts": _SLOTS["allowed_hosts"],
}


def test_build_generic_web_scrape_recipe_makes_zero_llm_calls() -> None:
    with patch(
        "elspeth.web.composer.service._litellm_acompletion",
        new_callable=AsyncMock,
    ) as mock_acomp:
        args = _build_web_scrape_project_recipe(_GENERIC_SLOTS)
        # llm node IS present in the COMPOSED pipeline (it runs at RUN time,
        # not compose time) — but the build itself called no provider.
        assert any(n["plugin"] == "llm" for n in args["nodes"])
    assert mock_acomp.call_count == 0


def test_apply_web_scrape_recipe_makes_zero_llm_calls() -> None:
    with patch(
        "elspeth.web.composer.service._litellm_acompletion",
        new_callable=AsyncMock,
    ) as mock_acomp:
        args = apply_recipe("web-scrape-llm-rate-jsonl", _SLOTS)
        assert any(node["plugin"] == "llm" for node in args["nodes"])
    assert mock_acomp.call_count == 0
