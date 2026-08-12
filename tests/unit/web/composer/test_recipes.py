"""Unit tests for ``src/elspeth/web/composer/recipes.py``.

Covers:

  * Slot validation: required vs optional slots, type coercion, error
    messaging that points the model toward the correct repair.
  * Specifically the URL-as-blob_id failure mode (recipe slot rejects a
    URL string with a hint to call create_blob first).
  * The two registered recipes (classify-rows-llm-jsonl,
    split-by-numeric-threshold) — generated set_pipeline args are
    structurally valid and reflect the supplied slots.
  * Registry surface (list_recipes, get_recipe).
"""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import replace
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

import elspeth.web.composer.recipes as recipes_module
from elspeth.core.expression_parser import ExpressionParser
from elspeth.plugins.infrastructure.discovery import create_dynamic_hookimpl
from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.plugins.sources.llm import LLMSource
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer.recipes import (
    RecipeIntentContext,
    RecipeOutputBinding,
    RecipeReviewedOutputSummary,
    RecipeReviewedSourceSummary,
    RecipeSourceBinding,
    RecipeSpec,
    RecipeValidationError,
    SlotSpec,
    apply_recipe,
    get_recipe,
    list_recipes,
    match_registered_recipe_intent,
    recipe_catalog_content_hash,
)
from elspeth.web.config import WebSettings
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig

# --------------------------------------------------------------------------
# Registry surface
# --------------------------------------------------------------------------


class _CompilerLLMSource(LLMSource):
    determinism = LLMSource.determinism
    source_file_hash = "sha256:0123456789abcdef"


def _isolated_manager_with_llm_source() -> PluginManager:
    manager = PluginManager()
    manager.register_builtin_plugins()
    if all(source.name != "llm" for source in manager.get_sources()):
        manager.register(create_dynamic_hookimpl([_CompilerLLMSource], "elspeth_get_source"))
    return manager


def _snapshot_with_llm_profiles(*aliases: str) -> PluginAvailabilitySnapshot:
    catalog = create_catalog_service()
    unrestricted = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    llm_id = PluginId("transform", "llm")
    return PluginAvailabilitySnapshot.create(
        policy_hash=unrestricted.policy_hash,
        principal_scope=unrestricted.principal_scope,
        available=unrestricted.available,
        unavailable=unrestricted.unavailable,
        selected=unrestricted.selected,
        usable_profile_aliases=((llm_id, aliases),),
        selected_profile_aliases=((llm_id, aliases[0] if aliases else None),),
        binding_generation_fingerprint=unrestricted.binding_generation_fingerprint,
    )


class TestRecipeRegistry:
    def test_registered_recipes(self) -> None:
        snapshot = _snapshot_with_llm_profiles("tutorial")
        names = {r["name"] for r in list_recipes(snapshot)}
        assert names == {
            "classify-rows-llm-jsonl",
            "split-by-numeric-threshold",
            "fork-coalesce-truncate-jsonl",
            "web-scrape-llm-project-jsonl",
            "web-scrape-llm-rate-jsonl",
        }

    def test_get_recipe_by_name(self) -> None:
        spec = get_recipe("classify-rows-llm-jsonl")
        assert spec is not None
        assert spec.name == "classify-rows-llm-jsonl"

    def test_get_recipe_unknown_returns_none(self) -> None:
        assert get_recipe("nonexistent") is None

    def test_list_recipes_includes_slot_metadata(self) -> None:
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())
        for entry in list_recipes(snapshot):
            assert "slots" in entry
            for _slot_name, slot_meta in entry["slots"].items():
                assert "type" in slot_meta
                assert "required" in slot_meta
                assert "description" in slot_meta

    def test_classify_recipe_advertises_only_public_profile_binding(self) -> None:
        snapshot = _snapshot_with_llm_profiles("tutorial")
        entry = next(item for item in list_recipes(snapshot) if item["name"] == "classify-rows-llm-jsonl")

        assert "profile" in entry["slots"]
        assert entry["slots"]["profile"]["choices"] == ["tutorial"]
        assert {"provider", "model", "api_key", "api_key_secret"}.isdisjoint(entry["slots"])

    def test_llm_recipes_are_hidden_without_usable_profile_aliases(self) -> None:
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())

        names = {entry["name"] for entry in list_recipes(snapshot)}

        assert "classify-rows-llm-jsonl" not in names
        assert "web-scrape-llm-rate-jsonl" not in names
        assert "split-by-numeric-threshold" in names

    def test_recipe_listing_excludes_dependencies_missing_from_snapshot(self) -> None:
        catalog = create_catalog_service()
        unrestricted = PluginAvailabilitySnapshot.for_trained_operator(catalog)
        snapshot = PluginAvailabilitySnapshot.create(
            policy_hash="recipe-policy",
            principal_scope="local:alice",
            available=unrestricted.available - {PluginId("transform", "llm")},
            unavailable=(),
            selected=unrestricted.selected,
            usable_profile_aliases=(),
            selected_profile_aliases=(),
            binding_generation_fingerprint="recipe-policy-generation",
        )

        names = {entry["name"] for entry in list_recipes(snapshot)}

        assert "classify-rows-llm-jsonl" not in names
        assert "web-scrape-llm-rate-jsonl" not in names
        assert "split-by-numeric-threshold" in names
        assert "fork-coalesce-truncate-jsonl" in names

    def test_dynamic_source_recipe_declares_csv_or_json_alternative(self) -> None:
        spec = get_recipe("web-scrape-llm-rate-jsonl")
        assert spec is not None
        assert spec.alternative_plugin_groups == (frozenset({PluginId("source", "csv"), PluginId("source", "json")}),)

    def test_recipe_owned_invalid_matcher_candidate_surfaces_validation_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        invalid_recipe = RecipeSpec(
            name="test-invalid-matcher-candidate",
            description="Test-only recipe with a contract-breaking matcher.",
            slots={"count": SlotSpec("int")},
            build=lambda _slots: {},
            matcher=lambda _context: recipes_module.RecipeIntentCandidate(slots={"count": "not-an-int"}),
        )
        monkeypatch.setitem(recipes_module._RECIPES, invalid_recipe.name, invalid_recipe)

        with pytest.raises(RecipeValidationError, match="slot 'count' must be an integer"):
            match_registered_recipe_intent(RecipeIntentContext(user_text="exercise the broken test matcher"))


class TestReviewedOutputProjectionCompatibility:
    def test_required_subset_is_compatible_regardless_of_projection_order(self) -> None:
        conflict = recipes_module.reviewed_output_projection_conflict(
            retained_fields=("abstract", "document_uri"),
            required_fields=("document_uri",),
        )

        assert conflict is None

    def test_projection_superset_is_compatible_without_widening_or_narrowing(self) -> None:
        retained = ("document_uri", "abstract", "published_at")

        conflict = recipes_module.reviewed_output_projection_conflict(
            retained_fields=retained,
            required_fields=("document_uri", "abstract"),
        )

        assert conflict is None
        assert retained == ("document_uri", "abstract", "published_at")

    def test_conflict_orders_and_deduplicates_missing_reviewed_fields_across_outputs(self) -> None:
        first = recipes_module.reviewed_output_projection_conflict(
            retained_fields=("abstract",),
            required_fields=("document_uri", "abstract", "published_at"),
        )
        second = recipes_module.reviewed_output_projection_conflict(
            retained_fields=("digest",),
            required_fields=("published_at", "source_id", "document_uri"),
        )

        missing = tuple(dict.fromkeys((*first.missing_fields, *second.missing_fields)))

        assert type(first) is recipes_module.ReviewedOutputProjectionConflict
        assert type(second) is recipes_module.ReviewedOutputProjectionConflict
        assert first.error_code == "reviewed_output_projection_conflict"
        assert missing == ("document_uri", "published_at", "source_id")


class TestRegisteredRecipeIntentMatching:
    def _context(
        self,
        text: str,
        *,
        source_name: str = "briefs",
        source_plugin: str = "json",
        locator_field: str = "document_uri",
        source_fields: tuple[str, ...] | None = None,
        response_field: str = "abstract",
        aliases: tuple[str, ...] = ("approved-summary",),
        required_fields: tuple[str, ...] | None = None,
        output_name: str = "projected",
        output_plugin: str = "json",
        output_path: str = "outputs/projected.jsonl",
        output_format: str = "jsonl",
    ) -> RecipeIntentContext:
        blob_id = str(uuid4())
        return RecipeIntentContext(
            user_text=text,
            reviewed_sources=(
                RecipeReviewedSourceSummary(
                    name=source_name,
                    plugin=source_plugin,
                    field_names=source_fields if source_fields is not None else (locator_field,),
                ),
            ),
            reviewed_outputs=(
                RecipeReviewedOutputSummary(
                    name=output_name,
                    plugin=output_plugin,
                    required_fields=required_fields if required_fields is not None else (locator_field, response_field),
                ),
            ),
            policy_snapshot=_snapshot_with_llm_profiles(*aliases),
            source_bindings=(
                RecipeSourceBinding(
                    name=source_name,
                    plugin=source_plugin,
                    blob_id=blob_id,
                ),
            ),
            output_bindings=(
                RecipeOutputBinding(
                    name=output_name,
                    plugin=output_plugin,
                    path=output_path,
                    format=output_format,
                ),
            ),
        )

    def test_invalid_server_owned_source_binding_surfaces_validation_failure(self) -> None:
        context = self._context(
            "Fetch every `document_uri`, have an LLM write an `abstract`, and retain exactly "
            "`document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )
        invalid_context = replace(
            context,
            source_bindings=(RecipeSourceBinding(name="briefs", plugin="json", blob_id="not-a-uuid"),),
        )

        with pytest.raises(RecipeValidationError, match=r"server-owned source binding.*valid UUID"):
            match_registered_recipe_intent(invalid_context)

    @pytest.mark.parametrize(
        ("text", "locator_field", "response_field"),
        [
            (
                "Fetch every page named by `document_uri`, have an LLM write a short `abstract`, "
                "and retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
                "scraping reason: 'Public-page abstract research'.",
                "document_uri",
                "abstract",
            ),
            (
                "Scrape each `page_ref`, use the LLM to produce `digest`, then keep only `page_ref` and `digest`. "
                "Abuse-contact: web-team@agency.gov.au; reason for the scrape is 'Accessibility research'.",
                "page_ref",
                "digest",
            ),
        ],
    )
    def test_generic_web_projection_matches_arbitrary_field_names(
        self,
        text: str,
        locator_field: str,
        response_field: str,
    ) -> None:
        match = match_registered_recipe_intent(self._context(text, locator_field=locator_field, response_field=response_field))

        assert match is not None
        assert match.recipe_name == "web-scrape-llm-project-jsonl"
        assert match.slots["locator_field"] == locator_field
        assert match.slots["response_field"] == response_field
        assert match.slots["retained_fields"] == (locator_field, response_field)
        assert match.slots["profile"] == "approved-summary"

    @pytest.mark.parametrize(
        ("text", "response_field", "semantic_phrase"),
        [
            (
                "Fetch every page named by `document_uri`, have an LLM write a short `abstract`, "
                "and retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
                "scraping reason: 'Public-page abstract research'.",
                "abstract",
                "write a short `abstract`",
            ),
            (
                "Fetch every page named by `document_uri`, use an LLM to produce a concise `digest`, "
                "and retain exactly `document_uri` and `digest`. Abuse contact: ops@agency.gov.au; "
                "scraping reason: 'Public-page digest research'.",
                "digest",
                "produce a concise `digest`",
            ),
        ],
    )
    def test_explicit_llm_semantics_become_prompt_without_metadata(
        self,
        text: str,
        response_field: str,
        semantic_phrase: str,
    ) -> None:
        match = match_registered_recipe_intent(self._context(text, response_field=response_field))

        assert match is not None
        prompt_template = match.slots["prompt_template"]
        assert type(prompt_template) is str
        assert semantic_phrase.casefold() in prompt_template.casefold()
        assert "{{ row['content'] }}" in prompt_template
        assert "retain" not in prompt_template.casefold()
        assert "ops@agency.gov.au" not in prompt_template
        assert "scraping reason" not in prompt_template.casefold()

    def test_semantic_constraint_after_semicolon_is_preserved_until_projection(self) -> None:
        context = self._context(
            "Fetch every `document_uri` and have an LLM write a short `abstract`; "
            "it must be no more than 50 words; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        prompt_template = match.slots["prompt_template"]
        assert type(prompt_template) is str
        assert "it must be no more than 50 words" in prompt_template
        assert "retain" not in prompt_template.casefold()

    def test_full_llm_semantic_prefix_and_suffix_are_preserved(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM compare the page's obligations with its evidence and "
            "write a concise `abstract` that begins with the decision and ends with citations; "
            "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        prompt_template = match.slots["prompt_template"]
        assert type(prompt_template) is str
        assert "compare the page's obligations with its evidence" in prompt_template
        assert "write a concise `abstract` that begins with the decision and ends with citations" in prompt_template
        assert "Fetch every" not in prompt_template

    def test_post_llm_cleanup_instruction_is_not_part_of_the_prompt(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`. Then remove raw page content and "
            "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        prompt_template = str(match.slots["prompt_template"])
        assert "write an `abstract`" in prompt_template
        assert "remove raw page content" not in prompt_template

    def test_semantic_content_removal_instruction_remains_in_the_prompt(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`. "
            "Then remove unsafe content from the abstract; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        prompt_template = str(match.slots["prompt_template"])
        assert "Then remove unsafe content from the abstract" in prompt_template

    def test_current_tutorial_transform_prompt_matches_through_arbitrary_reviewed_authority(self) -> None:
        tutorial_transforms_prompt = (
            "For each row, fetch the page at its `url`, then have an LLM write a short `summary`. "
            "Finally drop the raw HTML and fingerprint columns and retain exactly `url` and `summary`. "
            "Use noreply@dta.gov.au as the scraping abuse contact. "
            "Scraping reason: 'ELSPETH tutorial demonstration'."
        )
        context = self._context(
            tutorial_transforms_prompt,
            source_name="arbitrary-reviewed-pages",
            locator_field="url",
            response_field="summary",
            aliases=("arbitrary-approved-profile",),
            required_fields=("url", "summary"),
            output_name="arbitrary-reviewed-output",
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["profile"] == "arbitrary-approved-profile"
        prompt_template = str(match.slots["prompt_template"])
        assert "write a short `summary`" in prompt_template
        assert "drop the raw HTML" not in prompt_template

    @pytest.mark.parametrize(
        "text",
        [
            (
                "Fetch every `document_uri`; do not have an LLM write an `abstract`; "
                "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
                "scraping reason: 'Public analysis'."
            ),
            (
                "Fetch every `document_uri`; have an LLM write an `abstract`; "
                "do not retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
                "scraping reason: 'Public analysis'."
            ),
            (
                "Fetch every `document_uri`; have an LLM not write an `abstract`; "
                "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
                "scraping reason: 'Public analysis'."
            ),
            (
                "Fetch every `document_uri`; never let an LLM produce an `abstract`; "
                "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
                "scraping reason: 'Public analysis'."
            ),
            (
                "Fetch every `document_uri`; never let the LLM produce an `abstract`; "
                "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
                "scraping reason: 'Public analysis'."
            ),
            (
                "Fetch every `document_uri`; must not let an LLM write an `abstract`; "
                "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
                "scraping reason: 'Public analysis'."
            ),
            (
                "Fetch every `document_uri`; avoid having an LLM write an `abstract`; "
                "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
                "scraping reason: 'Public analysis'."
            ),
            (
                "Fetch every `document_uri`; without having an LLM write an `abstract`; "
                "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
                "scraping reason: 'Public analysis'."
            ),
            (
                "Fetch every `document_uri`; do not ask the LLM to write an `abstract`; "
                "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
                "scraping reason: 'Public analysis'."
            ),
            (
                "Fetch every `document_uri`; should not have an LLM write an `abstract`; "
                "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
                "scraping reason: 'Public analysis'."
            ),
            (
                "Fetch every `document_uri`; shouldn't have an LLM write an `abstract`; "
                "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
                "scraping reason: 'Public analysis'."
            ),
            (
                "Fetch every `document_uri`; cannot have an LLM write an `abstract`; "
                "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
                "scraping reason: 'Public analysis'."
            ),
            (
                "Fetch every `document_uri`; have an LLM write an `abstract`; "
                "shouldn't retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
                "scraping reason: 'Public analysis'."
            ),
        ],
    )
    def test_negated_response_or_projection_contract_falls_back(self, text: str) -> None:
        assert match_registered_recipe_intent(self._context(text)) is None

    def test_conflicting_response_contracts_fall_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`, have an LLM write an `abstract` and produce a `digest`, "
            "then retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_unknown_connector_between_response_values_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract` coupled with produce a `digest`; "
            "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_bare_response_value_in_adjacent_clause_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`. Else digest. "
            "Retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_unrelated_transition_after_response_remains_eligible(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`. "
            "Otherwise continue with the reviewed pipeline. Retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["response_field"] == "abstract"

    @pytest.mark.parametrize(
        "response_contract",
        [
            "have an LLM produce `abstract` or `digest`",
            "have an LLM produce `abstract`/`digest`",
        ],
    )
    def test_response_field_alternative_residue_falls_back(self, response_contract: str) -> None:
        context = self._context(
            f"Fetch every `document_uri`; {response_contract}; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    @pytest.mark.parametrize(
        "alternative",
        [
            "Alternatively produce a `digest`",
            "Otherwise produce a `digest`",
            "Or produce a `digest`",
            "Instead produce a `digest`",
            "Alternatively, produce a `digest`",
            "Otherwise: produce a `digest`",
        ],
    )
    def test_new_clause_response_alternative_falls_back(self, alternative: str) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`. "
            f"{alternative}. Retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_semantic_scrape_negation_remains_prompt_content(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract` explaining why teams must not scrape private sites; "
            "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert "must not scrape private sites" in str(match.slots["prompt_template"])

    @pytest.mark.parametrize(
        "response_contract",
        [
            "have an LLM use no jargon to write an `abstract`",
            "have an LLM not only write an `abstract` but also explain the result",
        ],
    )
    def test_object_and_idiom_negation_remain_response_semantics(self, response_contract: str) -> None:
        context = self._context(
            f"Fetch every `document_uri`; {response_contract}; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["response_field"] == "abstract"

    @pytest.mark.parametrize(
        "response_contract",
        [
            "refuse to have an LLM write an `abstract`",
            "have an LLM that refuses to write an `abstract`",
        ],
    )
    def test_response_denial_verbs_fall_back(self, response_contract: str) -> None:
        context = self._context(
            f"Fetch every `document_uri`; {response_contract}; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    @pytest.mark.parametrize(
        "response_contract",
        [
            "have an LLM write an `abstract` explaining how teams produce a digest",
            "have an LLM write an `abstract` explaining when to 'return a digest later'",
            "have an LLM write an `abstract` that says users create a report",
        ],
    )
    def test_subordinate_response_verbs_remain_prompt_semantics(self, response_contract: str) -> None:
        context = self._context(
            f"Fetch every `document_uri`; {response_contract}; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["response_field"] == "abstract"
        assert response_contract.split("an LLM ", 1)[1] in str(match.slots["prompt_template"])

    def test_conflicting_projection_clauses_fall_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`, have an LLM write an `abstract`, retain exactly "
            "`document_uri` and `abstract`, then keep only `abstract`. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_conflicting_abuse_contacts_fall_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`, have an LLM write an `abstract`, and retain exactly "
            "`document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
            "abuse contact: retired@agency.gov.au; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_conflicting_scraping_reasons_fall_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`, have an LLM write an `abstract`, and retain exactly "
            "`document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'; scraping reason: 'Private analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_single_negated_scraping_reason_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`, have an LLM write an `abstract`, and retain exactly "
            "`document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
            "do not use scraping reason: 'Private analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_should_not_scraping_reason_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; shouldn't use scraping reason: 'Private analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_negated_and_positive_identical_scraping_reason_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`, have an LLM write an `abstract`, and retain exactly "
            "`document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
            "do not use scraping reason: 'Public analysis'; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_negated_abuse_contact_falls_back_instead_of_selecting_it(self) -> None:
        context = self._context(
            "Fetch every `document_uri`, have an LLM write an `abstract`, and retain exactly "
            "`document_uri` and `abstract`. Do not use retired@agency.gov.au as abuse contact; "
            "abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_should_not_abuse_contact_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Shouldn't use retired@agency.gov.au as abuse contact; abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_single_negated_reverse_form_contact_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`, have an LLM write an `abstract`, and retain exactly "
            "`document_uri` and `abstract`. Do not use retired@agency.gov.au as abuse contact; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    @pytest.mark.parametrize(
        "contact_clause",
        [
            "There is no abuse contact ops@agency.gov.au",
            "This is not abuse contact ops@agency.gov.au",
        ],
    )
    def test_same_clause_contact_negation_falls_back(self, contact_clause: str) -> None:
        context = self._context(
            "Fetch every `document_uri`, have an LLM write an `abstract`, and retain exactly "
            f"`document_uri` and `abstract`. {contact_clause}; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    @pytest.mark.parametrize(
        "authority",
        [
            "Abuse contact: ops@agency.gov.au or backup@agency.gov.au; scraping reason: 'Public analysis'.",
            "Abuse contact: ops@agency.gov.au or the service desk; scraping reason: 'Public analysis'.",
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis' or 'Compliance testing'.",
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis' or the fallback reason.",
        ],
    )
    def test_contact_or_reason_alternative_residue_falls_back(self, authority: str) -> None:
        context = self._context(
            f"Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. {authority}"
        )

        assert match_registered_recipe_intent(context) is None

    def test_comma_separated_contact_alternative_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au, backup@agency.gov.au; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_descriptive_contact_residue_with_non_contact_or_remains_eligible(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au, monitored by security or legal; scraping reason: 'Public analysis'."
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["abuse_contact"] == "ops@agency.gov.au"

    def test_comma_separated_scraping_reason_alternative_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public', 'Private'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_conjoined_scraping_reason_alternative_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public' and 'Private'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_as_well_as_scraping_reason_alternative_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public' as well as 'Private'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_plus_scraping_reason_alternative_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis' plus 'Private analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    @pytest.mark.parametrize("connector", ["along with", "together with"])
    def test_second_quoted_scraping_reason_falls_back_independent_of_connector(self, connector: str) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            f"Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis' {connector} 'Private analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_negation_inside_quoted_scraping_reason_is_content(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Document why access is not restricted'."
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["scraping_reason"] == "Document why access is not restricted"

    def test_identical_repeated_contract_values_remain_unambiguous(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write a short `abstract`; write a short `abstract`; "
            "retain exactly `document_uri` and `abstract`; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'; scraping reason: 'Public analysis'."
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["response_field"] == "abstract"
        assert match.slots["retained_fields"] == ("document_uri", "abstract")
        assert match.slots["abuse_contact"] == "ops@agency.gov.au"
        assert match.slots["scraping_reason"] == "Public analysis"

    def test_missing_response_field_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri` and have an LLM analyze it; retain exactly `document_uri`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_complete_conflicting_reviewed_projection_raises_closed_conflict(self) -> None:
        context = self._context(
            "Fetch every `document_uri`, have an LLM write an `abstract`, and retain only `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        with pytest.raises(Exception) as raised:
            match_registered_recipe_intent(context)

        assert type(raised.value) is recipes_module.ReviewedOutputProjectionConflict
        assert raised.value.error_code == "reviewed_output_projection_conflict"
        assert raised.value.missing_fields == ("document_uri",)

    def test_incomplete_near_match_with_projection_conflict_still_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`, have an LLM write an `abstract`, and retain only `abstract`. Scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_multiple_usable_profiles_are_ambiguous(self) -> None:
        context = self._context(
            "Fetch every `document_uri`, write an `abstract`, and retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'.",
            aliases=("profile-a", "profile-b"),
        )

        assert match_registered_recipe_intent(context) is None

    def test_explicit_available_profile_selects_among_multiple(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "LLM profile: `profile-b`. Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'.",
            aliases=("profile-a", "profile-b"),
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["profile"] == "profile-b"

    def test_identical_repeated_explicit_profile_remains_unambiguous(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "LLM profile: `profile-b`; use the `profile-b` LLM profile. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'.",
            aliases=("profile-a", "profile-b"),
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["profile"] == "profile-b"

    @pytest.mark.parametrize(
        "profile_clause",
        [
            "Do not use the `profile-a` LLM profile; use the `profile-b` LLM profile.",
            "Use the `retired-profile` LLM profile.",
            "The source describes profile-a customers.",
        ],
    )
    def test_negated_unavailable_or_incidental_profile_does_not_select(self, profile_clause: str) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            f"{profile_clause} Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'.",
            aliases=("profile-a", "profile-b"),
        )

        assert match_registered_recipe_intent(context) is None

    def test_explicit_unavailable_profile_does_not_fall_through_to_sole_available_profile(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Use the `retired-profile` LLM profile. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'.",
        )

        assert match_registered_recipe_intent(context) is None

    def test_negated_sole_available_profile_does_not_select(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Do not use approved-summary. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_should_not_use_sole_available_profile_does_not_select(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Shouldn't use approved-summary. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_avoid_using_sole_available_profile_does_not_select(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Avoid using approved-summary. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_bare_unavailable_profile_authority_does_not_default(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Use profile-b. Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'.",
            aliases=("profile-a",),
        )

        assert match_registered_recipe_intent(context) is None

    def test_bare_profile_alternative_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Use profile-a or profile-b. Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'.",
            aliases=("profile-a", "profile-b"),
        )

        assert match_registered_recipe_intent(context) is None

    def test_conjoined_bare_profile_alternative_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Use profile-a and profile-b. Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'.",
            aliases=("profile-a", "profile-b"),
        )

        assert match_registered_recipe_intent(context) is None

    def test_separator_bounded_profile_alternative_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Use profile-a; otherwise profile-b. Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'.",
            aliases=("profile-a", "profile-b"),
        )

        assert match_registered_recipe_intent(context) is None

    def test_punctuated_profile_alternative_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Use profile-a; otherwise, profile-b. Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'.",
            aliases=("profile-a", "profile-b"),
        )

        assert match_registered_recipe_intent(context) is None

    def test_profile_value_in_adjacent_clause_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Use profile-a. Backup: profile-b. Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'.",
            aliases=("profile-a", "profile-b"),
        )

        assert match_registered_recipe_intent(context) is None

    def test_profile_denial_verb_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Reject using approved-summary. Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    @pytest.mark.parametrize("denial", ["Reject", "Exclude"])
    def test_bare_profile_denial_falls_back(self, denial: str) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            f"{denial} profile-a. Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'.",
            aliases=("profile-a",),
        )

        assert match_registered_recipe_intent(context) is None

    def test_unknown_connector_between_profile_values_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Use profile-a coupled with profile-b. Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'.",
            aliases=("profile-a", "profile-b"),
        )

        assert match_registered_recipe_intent(context) is None

    def test_profile_like_prose_inside_quoted_reason_is_not_authority(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Explain why teams use profile-b'.",
            aliases=("profile-a",),
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["profile"] == "profile-a"

    def test_profile_like_subordinate_response_prose_is_not_authority(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract` explaining why teams use profile-b; "
            "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'.",
            aliases=("profile-a",),
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["profile"] == "profile-a"
        assert "teams use profile-b" in str(match.slots["prompt_template"])

    def test_recommended_profile_like_response_prose_is_not_authority(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract` recommending teams use profile-b; "
            "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'.",
            aliases=("profile-a",),
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["profile"] == "profile-a"
        assert "recommending teams use profile-b" in str(match.slots["prompt_template"])

    def test_named_profile_authority_before_response_remains_eligible(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; use an LLM profile named approved-summary to write an `abstract`; "
            "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["profile"] == "approved-summary"
        assert "profile named" not in str(match.slots["prompt_template"]).casefold()

    def test_conflicting_explicit_profiles_fall_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "LLM profile: `profile-a`; use the `profile-b` LLM profile. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'.",
            aliases=("profile-a", "profile-b"),
        )

        assert match_registered_recipe_intent(context) is None

    def test_alias_inside_quoted_reason_is_not_profile_authority(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Evaluate profile-a migration evidence'.",
            aliases=("profile-a", "profile-b"),
        )

        assert match_registered_recipe_intent(context) is None

    def test_multiple_source_bindings_are_ambiguous(self) -> None:
        context = self._context(
            "Fetch every `document_uri`, write an `abstract`, and retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )
        context = RecipeIntentContext(
            user_text=context.user_text,
            reviewed_sources=context.reviewed_sources,
            reviewed_outputs=context.reviewed_outputs,
            policy_snapshot=context.policy_snapshot,
            source_bindings=(
                *context.source_bindings,
                RecipeSourceBinding(name="other", plugin="json", blob_id=str(uuid4())),
            ),
            output_bindings=context.output_bindings,
        )

        assert match_registered_recipe_intent(context) is None

    def test_multiple_output_bindings_are_ambiguous(self) -> None:
        context = self._context(
            "Fetch every `document_uri`, write an `abstract`, and retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )
        context = RecipeIntentContext(
            user_text=context.user_text,
            reviewed_sources=context.reviewed_sources,
            reviewed_outputs=context.reviewed_outputs,
            policy_snapshot=context.policy_snapshot,
            source_bindings=context.source_bindings,
            output_bindings=(
                *context.output_bindings,
                RecipeOutputBinding(name="other", plugin="json", path="outputs/other.jsonl", format="jsonl"),
            ),
        )

        assert match_registered_recipe_intent(context) is None

    @pytest.mark.parametrize(
        "text",
        [
            (
                "Fetch every `document_uri`, write an `abstract`, and retain exactly `document_uri` and `abstract`. "
                "Scraping reason: 'Public analysis'."
            ),
            (
                "Fetch every `document_uri`, write an `abstract`, and retain exactly `document_uri` and `abstract`. "
                "Abuse contact: ops@agency.gov.au."
            ),
        ],
    )
    def test_missing_http_identity_falls_back(self, text: str) -> None:
        assert match_registered_recipe_intent(self._context(text)) is None

    @pytest.mark.parametrize(
        "authority_clause",
        [
            "Do not fetch the reviewed `document_uri` source rows.",
            "Use a CSV source for these rows.",
            "Do not use the reviewed JSON source `briefs`.",
            "Do not write a JSONL output.",
            "Write a CSV output instead.",
            "Do not use the reviewed output `projected`.",
            "Output must not be JSONL.",
            "Output JSON rather than JSONL.",
            "Shouldn't fetch the reviewed `document_uri` source rows.",
            "Shouldn't use the reviewed JSON source `briefs`.",
            "Should not write the reviewed JSONL output `projected`.",
            "Cannot fetch the reviewed `document_uri` source rows.",
            "Cannot use the reviewed JSON source `briefs`.",
            "Refuse to fetch the reviewed `document_uri` source rows.",
            "Decline to use the reviewed JSON source `briefs`.",
            "Exclude using the reviewed JSON source `briefs`.",
        ],
    )
    def test_source_or_output_authority_contradiction_falls_back(self, authority_clause: str) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            f"{authority_clause} Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    @pytest.mark.parametrize(
        "authority_clause",
        [
            "Use either JSON or CSV source.",
            "Use JSON/CSV source.",
            "Write either JSONL or CSV output.",
            "Write JSONL/CSV output.",
            "Reject JSON source.",
            "Exclude JSON source.",
            "Reject JSONL output.",
            "Exclude JSONL output.",
        ],
    )
    def test_unmatched_slot_shaped_authority_falls_back(self, authority_clause: str) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            f"{authority_clause} Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_matching_explicit_source_and_output_authority_remains_eligible(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Use the reviewed JSON source `briefs` and write the reviewed JSONL output `projected`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is not None

    def test_output_negation_outside_authority_span_remains_eligible(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Write reviewed JSONL output `projected`, but abstract must not contain JSON. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is not None

    def test_source_negation_outside_authority_span_remains_eligible(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Use reviewed JSON source `briefs`, not raw HTML source material. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is not None

    @pytest.mark.parametrize(
        "authority_clause",
        [
            "Use reviewed JSON source `briefs`. Otherwise continue with the reviewed pipeline.",
            "Write reviewed JSONL output `projected`. Otherwise continue with the reviewed pipeline.",
        ],
    )
    def test_unrelated_transition_after_source_or_output_remains_eligible(self, authority_clause: str) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            f"{authority_clause} Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is not None

    @pytest.mark.parametrize(
        "authority_clause",
        [
            "Write output at outputs/other.jsonl.",
            "Write a YAML output.",
            "Write a Parquet output.",
            "Use reviewed output other.",
            "Use a YAML source.",
            "Use reviewed source other.",
        ],
    )
    def test_explicit_unsupported_or_conflicting_authority_falls_back(self, authority_clause: str) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            f"{authority_clause} Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_matching_explicit_output_path_remains_eligible(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Write output at outputs/projected.jsonl. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is not None

    def test_conflicting_source_authority_alternative_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Use JSON source briefs or CSV source backup. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_otherwise_source_authority_alternative_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Use JSON source briefs otherwise CSV source backup. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_separator_bounded_source_authority_alternative_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Use JSON source briefs; otherwise CSV source backup. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_punctuated_source_authority_alternative_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Use JSON source briefs; otherwise, CSV source backup. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_source_value_in_adjacent_clause_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Use JSON source briefs. Else CSV. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_unknown_connector_between_source_values_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Use JSON source briefs coupled with CSV source backup. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_separator_bounded_output_authority_alternative_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Write JSONL output projected; otherwise CSV output backup. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_punctuated_output_authority_alternative_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Write JSONL output projected; otherwise, CSV output backup. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_output_value_in_adjacent_clause_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Write JSONL output projected. As an alternative, CSV. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_unknown_connector_between_output_values_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Write JSONL output projected coupled with CSV output backup. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_separate_clause_output_path_alternative_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Write output at outputs/projected.jsonl. Or write it to outputs/other.jsonl. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_subordinate_source_prose_is_not_source_authority(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract` explaining how teams use a CSV source; "
            "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert "teams use a CSV source" in str(match.slots["prompt_template"])

    def test_unparsed_source_and_output_choices_in_response_prose_are_not_authority(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract` explaining why teams use either JSON or CSV "
            "source and write either JSONL or CSV output; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert "use either JSON or CSV source" in str(match.slots["prompt_template"])

    def test_bare_profile_denial_in_response_prose_is_not_profile_authority(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract` explaining why teams reject profile-a; "
            "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'.",
            aliases=("profile-a",),
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["profile"] == "profile-a"

    def test_negative_cleanup_does_not_negate_exact_projection(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; "
            "retain exactly `document_uri` and `abstract`, but do not retain raw content. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["retained_fields"] == ("document_uri", "abstract")

    def test_prior_negative_cleanup_does_not_negate_exact_projection(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; "
            "do not retain raw content, but retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["retained_fields"] == ("document_uri", "abstract")

    def test_omit_rather_than_retain_exact_projection_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; "
            "omit rather than retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    @pytest.mark.parametrize(
        "identity_clauses",
        [
            "Omit abuse contact. Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'.",
            "Abuse contact: ops@agency.gov.au; omit scraping reason. Scraping reason: 'Public analysis'.",
        ],
    )
    def test_omitted_http_identity_falls_back(self, identity_clauses: str) -> None:
        context = self._context(
            f"Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. {identity_clauses}"
        )

        assert match_registered_recipe_intent(context) is None

    @pytest.mark.parametrize(
        "transition_position",
        [
            "Abuse contact: ops@agency.gov.au. Otherwise continue with the reviewed pipeline. Scraping reason: 'Public analysis'.",
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'. Otherwise continue with the reviewed pipeline.",
        ],
    )
    def test_unrelated_transition_after_http_identity_remains_eligible(self, transition_position: str) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            f"{transition_position}"
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["abuse_contact"] == "ops@agency.gov.au"
        assert match.slots["scraping_reason"] == "Public analysis"

    def test_affirmative_output_must_format_remains_eligible(self) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Output must be JSONL. Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is not None

    @pytest.mark.parametrize(
        "reason_clause",
        [
            "scraping reason: 'Audit `document_uri` availability'",
            "reason for the scrape is 'Audit `document_uri` availability'",
        ],
    )
    def test_locator_is_not_inferred_from_incidental_quoted_scraping_reason(self, reason_clause: str) -> None:
        context = self._context(
            "Fetch every reviewed page; have an LLM write an `abstract`; retain only `abstract`. "
            f"Abuse contact: ops@agency.gov.au; {reason_clause}.",
            source_fields=("document_uri", "canonical_uri"),
            required_fields=("abstract",),
        )

        assert match_registered_recipe_intent(context) is None

    def test_conflicting_explicit_locator_does_not_fall_through_to_projection_field(self) -> None:
        context = self._context(
            "Fetch every `page_ref`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'.",
            source_fields=("document_uri",),
        )

        assert match_registered_recipe_intent(context) is None

    def test_tenant_qualifier_is_not_inferred_as_locator(self) -> None:
        context = self._context(
            "Fetch every reviewed page for tenant `tenant_id`; have an LLM write an `abstract`; "
            "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'.",
            source_fields=("document_uri", "tenant_id"),
        )

        assert match_registered_recipe_intent(context) is None

    def test_explicit_locator_with_backticked_tenant_qualifier_remains_eligible(self) -> None:
        context = self._context(
            "Fetch every page at `document_uri` for tenant `tenant_id`; have an LLM write an `abstract`; "
            "retain exactly `document_uri` and `abstract`. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'.",
            source_fields=("document_uri", "tenant_id"),
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["locator_field"] == "document_uri"

    @pytest.mark.parametrize(
        "generated_field",
        ["content", "content_fingerprint", "fetch_status", "fetch_url_final", "fetch_url_final_ip"],
    )
    def test_any_upstream_web_scrape_generated_field_collision_falls_back(self, generated_field: str) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'.",
            source_fields=("document_uri", generated_field),
        )

        assert match_registered_recipe_intent(context) is None

    @pytest.mark.parametrize(
        "generated_field",
        ["content", "content_fingerprint", "fetch_status", "fetch_url_final", "fetch_url_final_ip"],
    )
    def test_locator_cannot_collide_with_a_web_scrape_generated_field(self, generated_field: str) -> None:
        context = self._context(
            f"Fetch every `{generated_field}`; have an LLM write an `abstract`; "
            f"retain exactly `{generated_field}` and `abstract`. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'.",
            locator_field=generated_field,
        )

        assert match_registered_recipe_intent(context) is None

    @pytest.mark.parametrize(
        "response_field",
        ["content", "content_fingerprint", "fetch_status", "fetch_url_final", "fetch_url_final_ip"],
    )
    def test_llm_response_cannot_collide_with_a_web_scrape_generated_field(self, response_field: str) -> None:
        context = self._context(
            f"Fetch every `document_uri`; have an LLM write `{response_field}`; "
            f"retain exactly `document_uri` and `{response_field}`. Abuse contact: ops@agency.gov.au; "
            "scraping reason: 'Public analysis'.",
            response_field=response_field,
        )

        assert match_registered_recipe_intent(context) is None

    @pytest.mark.parametrize("source_collision", ["abstract", "abstract_usage", "abstract_model"])
    def test_upstream_llm_generated_namespace_collision_falls_back(self, source_collision: str) -> None:
        context = self._context(
            "Fetch every `document_uri`; have an LLM write an `abstract`; retain exactly `document_uri` and `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'.",
            source_fields=("document_uri", source_collision),
        )

        assert match_registered_recipe_intent(context) is None

    def test_production_matcher_has_no_tutorial_identity_dependency(self) -> None:
        import elspeth.web.composer.recipe_intent_routing as routing_module

        source = inspect.getsource(recipes_module._match_web_scrape_project_intent) + inspect.getsource(routing_module)

        assert "tutorial" not in source.casefold()
        assert "session_id" not in source
        assert "TUTORIAL_" not in source

    def test_retained_fields_come_only_from_the_explicit_projection_clause(self) -> None:
        context = self._context(
            "Fetch every `document_uri`, use an LLM to write `abstract`, and retain only `abstract`. "
            "Abuse contact: ops@agency.gov.au; scraping reason: 'Public analysis'.",
            required_fields=("abstract",),
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["retained_fields"] == ("abstract",)

    def test_unrelated_email_does_not_override_explicit_abuse_contact(self) -> None:
        context = self._context(
            "Fetch every `document_uri`, have an LLM write an `abstract`, and retain exactly "
            "`document_uri` and `abstract`. Send results to analyst@agency.gov.au. "
            "Abuse contact: crawler-ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["abuse_contact"] == "crawler-ops@agency.gov.au"

    def test_unrelated_email_without_explicit_abuse_contact_falls_back(self) -> None:
        context = self._context(
            "Fetch every `document_uri`, have an LLM write an `abstract`, and retain exactly "
            "`document_uri` and `abstract`. Send results to analyst@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    def test_scraping_abuse_contact_suffix_is_explicit_contact_syntax(self) -> None:
        context = self._context(
            "Fetch every `document_uri`, have an LLM write an `abstract`, and retain exactly "
            "`document_uri` and `abstract`. Use crawler-ops@agency.gov.au as the scraping abuse contact; "
            "scraping reason: 'Public analysis'."
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots["abuse_contact"] == "crawler-ops@agency.gov.au"

    def test_unknown_explicitly_retained_field_falls_back_instead_of_narrowing(self) -> None:
        context = self._context(
            "Fetch every `document_uri`, have an LLM write an `abstract`, and retain exactly "
            "`document_uri`, `abstract`, and `mystery`. Abuse contact: crawler-ops@agency.gov.au; "
            "scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    @pytest.mark.parametrize(
        "projection",
        [
            "`document_uri`, `abstract`, and mystery",
            "`document_uri`, `abstract`, plus mystery",
            "`document_uri`, `abstract`, and `mystery-field`",
        ],
    )
    def test_unparsed_projection_residue_falls_back_instead_of_narrowing(self, projection: str) -> None:
        context = self._context(
            f"Fetch every `document_uri`, have an LLM write an `abstract`, and retain exactly {projection}. "
            "Abuse contact: crawler-ops@agency.gov.au; scraping reason: 'Public analysis'."
        )

        assert match_registered_recipe_intent(context) is None

    @pytest.mark.parametrize(
        ("output_path", "output_format"),
        [
            ("outputs/projected.json", "json"),
            ("outputs/projected.jsonl", "json"),
        ],
    )
    def test_jsonl_recipe_falls_back_for_effective_json_output(self, output_path: str, output_format: str) -> None:
        context = self._context(
            "Fetch every `document_uri`, have an LLM write an `abstract`, and retain exactly "
            "`document_uri` and `abstract`. Abuse contact: crawler-ops@agency.gov.au; "
            "scraping reason: 'Public analysis'.",
            output_path=output_path,
            output_format=output_format,
        )

        assert match_registered_recipe_intent(context) is None

    def test_fork_recipe_equal_visible_merge_keys_fall_back(self) -> None:
        context = RecipeIntentContext(
            user_text=(
                "Process these rows two ways in parallel and produce a single merged output row. "
                "Path B truncates the description field to 30 characters. Combine the branches "
                "under separate keys `same` and `same`. Customer rows (CSV):\n"
                "name,description\nalice,a long description"
            )
        )

        assert match_registered_recipe_intent(context) is None


class TestForkIntentAdversarialMatrix:
    @staticmethod
    def _context(*clauses: str) -> RecipeIntentContext:
        return RecipeIntentContext(
            user_text=(
                "Process these rows two ways in parallel and produce a single merged output row. "
                + " ".join(clauses)
                + " Customer rows (CSV):\nname,description\nalice,a long description"
            )
        )

    @pytest.mark.parametrize(
        "clauses",
        [
            ("Path B truncates the description field to 30 characters.", "Path B truncates the name field to 20 characters."),
            ("Path B truncates the description field to 30 characters. Write it at outputs/a.jsonl.", "Write it at outputs/b.jsonl."),
            ("Path B truncates the description field to 30 characters. Use suffix '...'.", "Use suffix '---'."),
            (
                "Path B truncates the description field to 30 characters. Combine under separate keys `full` and `short`.",
                "Combine under separate keys `original` and `trimmed`.",
            ),
        ],
    )
    def test_conflicting_repeated_semantic_slots_fall_back(self, clauses: tuple[str, ...]) -> None:
        assert match_registered_recipe_intent(self._context(*clauses)) is None

    @pytest.mark.parametrize(
        "clauses",
        [
            (
                "Do not process these rows two ways in parallel or produce a single merged output row.",
                "Path B truncates the description field to 30 characters.",
            ),
            ("Path B does not truncate the description field to 30 characters.",),
            ("Path B truncates the description field to 30 characters.", "Do not write it at outputs/a.jsonl."),
            ("Path B truncates the description field to 30 characters.", "Do not use suffix '...'."),
            (
                "Path B truncates the description field to 30 characters.",
                "Do not combine under separate keys `full` and `short`.",
            ),
            (
                "Shouldn't process these rows two ways in parallel or produce a single merged output row.",
                "Path B truncates the description field to 30 characters.",
            ),
            ("Path B shouldn't truncate the description field to 30 characters.",),
            ("Path B truncates the description field to 30 characters.", "Should not write it at outputs/a.jsonl."),
            ("Path B truncates the description field to 30 characters.", "Shouldn't use suffix '...'."),
            (
                "Path B truncates the description field to 30 characters.",
                "Should not combine under separate keys `full` and `short`.",
            ),
        ],
    )
    def test_negated_semantic_slots_fall_back(self, clauses: tuple[str, ...]) -> None:
        assert match_registered_recipe_intent(self._context(*clauses)) is None

    def test_unrelated_prior_negation_does_not_negate_fork_authority(self) -> None:
        context = RecipeIntentContext(
            user_text=(
                "Do not fake the split with a transform, process these rows two ways in parallel and produce "
                "a single merged output row. Path B truncates the description field to 30 characters. "
                "Customer rows (CSV):\nname,description\nalice,a long description"
            )
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots == {"truncate_field": "description", "max_chars": 30}

    def test_object_negation_before_truncate_remains_eligible(self) -> None:
        context = self._context("Path B uses no padding before it truncates the description field to 30 characters.")

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots == {"truncate_field": "description", "max_chars": 30}

    @pytest.mark.parametrize(
        "clauses",
        [
            (
                "Refuse to process these rows two ways in parallel or produce a single merged output row.",
                "Path B truncates the description field to 30 characters.",
            ),
            (
                "Path B rejects truncating the description field to 30 characters.",
                "Path B truncates the description field to 30 characters.",
            ),
        ],
    )
    def test_denial_verbs_governing_fork_actions_fall_back(self, clauses: tuple[str, ...]) -> None:
        assert match_registered_recipe_intent(self._context(*clauses)) is None

    def test_identical_repeated_semantic_slots_remain_unambiguous(self) -> None:
        context = self._context(
            "Process these rows two ways in parallel and produce a single merged output row.",
            "Path B truncates the description field to 30 characters.",
            "Path B truncates the description field to 30 characters.",
            "Write it at outputs/merged.jsonl. Write it at outputs/merged.jsonl.",
            "Use suffix '...'. Use suffix '...'.",
            "Combine under separate keys `full` and `short`. Combine under separate keys `full` and `short`.",
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots == {
            "truncate_field": "description",
            "max_chars": 30,
            "output_path": "outputs/merged.jsonl",
            "truncation_suffix": "...",
            "key_a": "full",
            "key_b": "short",
        }

    def test_conflicting_repeated_inline_csv_source_falls_back(self) -> None:
        context = RecipeIntentContext(
            user_text=(
                "Process these rows two ways in parallel and produce a single merged output row. "
                "Path B truncates the description field to 30 characters. Customer rows (CSV):\n"
                "name,description\nalice,first\nCustomer rows (CSV):\nname,description\nbob,second"
            )
        )

        assert match_registered_recipe_intent(context) is None

    def test_identical_repeated_inline_csv_source_remains_unambiguous(self) -> None:
        context = RecipeIntentContext(
            user_text=(
                "Process these rows two ways in parallel and produce a single merged output row. "
                "Path B truncates the description field to 30 characters. Customer rows (CSV):\n"
                "name,description\nalice,first\nCustomer rows (CSV):\nname,description\nalice,first"
            )
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.inline_blob is not None
        assert match.inline_blob.content == "name,description\nalice,first"

    def test_negated_inline_csv_source_falls_back(self) -> None:
        context = RecipeIntentContext(
            user_text=(
                "Process these rows two ways in parallel and produce a single merged output row. "
                "Path B truncates the description field to 30 characters. Do not use these customer rows (CSV):\n"
                "name,description\nalice,first"
            )
        )

        assert match_registered_recipe_intent(context) is None

    def test_inline_csv_cells_cannot_set_recipe_authority(self) -> None:
        context = RecipeIntentContext(
            user_text=(
                "Process these rows two ways in parallel and produce a single merged output row. "
                "Path B truncates the description field to 30 characters. Customer rows (CSV):\n"
                "name,description\n"
                "alice,at outputs/evil.jsonl\n"
                "bob,suffix '--'\n"
                "carol,under separate keys `evil` and `bad`\n"
                "dave,Path B shouldn't truncate the description field to 30 characters"
            )
        )

        match = match_registered_recipe_intent(context)

        assert match is not None
        assert match.slots == {"truncate_field": "description", "max_chars": 30}

    def test_negated_then_positive_truncate_authority_falls_back(self) -> None:
        context = self._context("Path B must not truncate; path B truncates the description field to 30 characters.")

        assert match_registered_recipe_intent(context) is None

    def test_inline_output_path_alternative_falls_back(self) -> None:
        context = self._context("Path B truncates the description field to 30 characters. Write it at outputs/a.jsonl or outputs/b.jsonl.")

        assert match_registered_recipe_intent(context) is None

    def test_inline_suffix_alternative_falls_back(self) -> None:
        context = self._context("Path B truncates the description field to 30 characters. Use suffix '...' or '--'.")

        assert match_registered_recipe_intent(context) is None

    def test_unparsed_alternative_merge_keys_fall_back(self) -> None:
        context = self._context(
            "Path B truncates the description field to 30 characters. "
            "Combine under separate keys `full` and `short`. Alternatively use keys `full` and `brief`."
        )

        assert match_registered_recipe_intent(context) is None

    def test_inline_merge_key_alternative_falls_back(self) -> None:
        context = self._context(
            "Path B truncates the description field to 30 characters. "
            "Combine under separate keys `full` and `short` or `original` and `trimmed`."
        )

        assert match_registered_recipe_intent(context) is None

    @pytest.mark.parametrize(
        "alternative",
        ["Alternatively outputs/b.jsonl", "Or outputs/b.jsonl", "Otherwise, outputs/b.jsonl"],
    )
    def test_separate_clause_output_path_alternative_falls_back(self, alternative: str) -> None:
        context = self._context(f"Path B truncates the description field to 30 characters. Write it at outputs/a.jsonl. {alternative}.")

        assert match_registered_recipe_intent(context) is None

    def test_adjacent_clause_output_path_value_falls_back(self) -> None:
        context = self._context(
            "Path B truncates the description field to 30 characters. Write it at outputs/a.jsonl. Backup: outputs/b.jsonl."
        )

        assert match_registered_recipe_intent(context) is None

    @pytest.mark.parametrize("alternative", ["Alternatively '--'", "Or '--'"])
    def test_separate_clause_suffix_alternative_falls_back(self, alternative: str) -> None:
        context = self._context(f"Path B truncates the description field to 30 characters. Use suffix '...'. {alternative}.")

        assert match_registered_recipe_intent(context) is None

    def test_adjacent_clause_suffix_value_falls_back(self) -> None:
        context = self._context("Path B truncates the description field to 30 characters. Use suffix '...'. Backup: '--'.")

        assert match_registered_recipe_intent(context) is None


class TestGenericWebProjectionRecipe:
    @staticmethod
    def _slots(**overrides: object) -> dict[str, object]:
        return {
            "source_blob_id": str(uuid4()),
            "source_plugin": "json",
            "locator_field": "page_ref",
            "profile": "approved-summary",
            "response_field": "digest",
            "required_input_fields": ["content"],
            "retained_fields": ["page_ref", "digest"],
            "abuse_contact": "web-team@agency.gov.au",
            "scraping_reason": "Public accessibility research",
            "output_path": "outputs/digests.jsonl",
            **overrides,
        }

    def test_build_uses_exact_arbitrary_projection(self) -> None:
        args = apply_recipe(
            "web-scrape-llm-project-jsonl",
            self._slots(),
        )

        web_scrape, llm, field_mapper = args["nodes"]
        assert web_scrape["options"]["url_field"] == "page_ref"
        assert llm["options"]["response_field"] == "digest"
        assert field_mapper["options"]["mapping"] == {"page_ref": "page_ref", "digest": "digest"}

    @pytest.mark.parametrize("raw_field", ["content", "content_fingerprint"])
    def test_raw_scrape_fields_cannot_be_retained(self, raw_field: str) -> None:
        with pytest.raises(RecipeValidationError, match="raw scrape"):
            apply_recipe(
                "web-scrape-llm-project-jsonl",
                {
                    "source_blob_id": str(uuid4()),
                    "source_plugin": "json",
                    "locator_field": "page_ref",
                    "profile": "approved-summary",
                    "response_field": "digest",
                    "retained_fields": ["page_ref", "digest", raw_field],
                    "abuse_contact": "web-team@agency.gov.au",
                    "scraping_reason": "Public accessibility research",
                    "output_path": "outputs/digests.jsonl",
                },
            )

    @pytest.mark.parametrize(
        "generated_field",
        ["content", "content_fingerprint", "fetch_status", "fetch_url_final", "fetch_url_final_ip"],
    )
    def test_builder_rejects_locator_collision_with_every_web_scrape_generated_field(self, generated_field: str) -> None:
        with pytest.raises(RecipeValidationError, match="generated field namespace"):
            apply_recipe(
                "web-scrape-llm-project-jsonl",
                self._slots(
                    locator_field=generated_field,
                    retained_fields=[generated_field, "digest"],
                ),
            )

    @pytest.mark.parametrize(
        "generated_field",
        ["content", "content_fingerprint", "fetch_status", "fetch_url_final", "fetch_url_final_ip"],
    )
    def test_builder_rejects_response_collision_with_every_web_scrape_generated_field(self, generated_field: str) -> None:
        with pytest.raises(RecipeValidationError, match="generated field namespace"):
            apply_recipe(
                "web-scrape-llm-project-jsonl",
                self._slots(
                    response_field=generated_field,
                    retained_fields=["page_ref", generated_field],
                ),
            )

    @pytest.mark.parametrize("colliding_field", ["digest_usage", "digest_model"])
    def test_builder_rejects_retained_source_collision_with_llm_metadata(self, colliding_field: str) -> None:
        with pytest.raises(RecipeValidationError, match="generated field namespace"):
            apply_recipe(
                "web-scrape-llm-project-jsonl",
                self._slots(retained_fields=["page_ref", colliding_field, "digest"]),
            )

    @pytest.mark.parametrize("locator_field", ["digest", "digest_usage", "digest_model"])
    def test_builder_rejects_locator_collision_with_llm_generated_namespace(self, locator_field: str) -> None:
        with pytest.raises(RecipeValidationError, match="generated field namespace"):
            apply_recipe(
                "web-scrape-llm-project-jsonl",
                self._slots(locator_field=locator_field, retained_fields=[locator_field, "digest"]),
            )

    def test_legacy_rating_is_a_specialization_of_generic_graph(self) -> None:
        source_blob_id = str(uuid4())
        legacy = apply_recipe(
            "web-scrape-llm-rate-jsonl",
            {
                "source_blob_id": source_blob_id,
                "source_plugin": "json",
                "profile": "approved-rating",
                "abuse_contact": "web-team@agency.gov.au",
                "scraping_reason": "Public page rating",
            },
        )
        generic = apply_recipe(
            "web-scrape-llm-project-jsonl",
            {
                "source_blob_id": source_blob_id,
                "source_plugin": "json",
                "locator_field": "url",
                "profile": "approved-rating",
                "prompt_template": ("Rate the appeal of this government web page from 1-10 and explain briefly:\n\n{{ row['content'] }}"),
                "response_field": "rating",
                "required_input_fields": ["content"],
                "retained_fields": ["url", "rating"],
                "abuse_contact": "web-team@agency.gov.au",
                "scraping_reason": "Public page rating",
                "output_path": "outputs/ratings.jsonl",
            },
        )

        assert legacy == generic


# --------------------------------------------------------------------------
# SlotSpec construction: default value validated against slot_type
# --------------------------------------------------------------------------


class TestSlotSpecDefaultValidation:
    """Recipe authors must declare defaults that satisfy the slot_type contract.

    A typo like ``SlotSpec(slot_type="int", required=False, default="oops")``
    must crash at construction (i.e. at recipe module import) rather than at
    recipe-application time on a path the recipe's tests may not exercise.
    """

    def test_int_default_must_be_int(self) -> None:
        with pytest.raises(ValueError, match="does not satisfy slot_type 'int'"):
            SlotSpec(slot_type="int", required=False, default="oops")

    def test_int_default_int_passes(self) -> None:
        SlotSpec(slot_type="int", required=False, default=42)

    def test_int_default_bool_rejected(self) -> None:
        # bool is a subclass of int in Python — _coerce_slot rejects it.
        with pytest.raises(ValueError, match="does not satisfy slot_type 'int'"):
            SlotSpec(slot_type="int", required=False, default=True)

    def test_float_default_must_be_numeric(self) -> None:
        with pytest.raises(ValueError, match="does not satisfy slot_type 'float'"):
            SlotSpec(slot_type="float", required=False, default=[])

    def test_float_default_int_coerces(self) -> None:
        # _coerce_slot accepts int as a float — mirroring the runtime behaviour.
        SlotSpec(slot_type="float", required=False, default=1)

    def test_str_default_must_be_str(self) -> None:
        with pytest.raises(ValueError, match="does not satisfy slot_type 'str'"):
            SlotSpec(slot_type="str", required=False, default=42)

    def test_blob_id_default_must_be_uuid(self) -> None:
        with pytest.raises(ValueError, match="does not satisfy slot_type 'blob_id'"):
            SlotSpec(slot_type="blob_id", required=False, default="not-a-uuid")

    def test_str_list_default_must_be_iterable_of_str(self) -> None:
        with pytest.raises(ValueError, match="does not satisfy slot_type 'str_list'"):
            SlotSpec(slot_type="str_list", required=False, default="single-string")

    def test_str_list_default_empty_tuple_passes(self) -> None:
        # The recipe registry uses ``default=()`` for required_input_fields —
        # this is the documented "explicit opt-out" path and must remain valid.
        SlotSpec(slot_type="str_list", required=False, default=())

    def test_str_list_default_with_strs_passes(self) -> None:
        SlotSpec(slot_type="str_list", required=False, default=("a", "b"))

    def test_str_list_default_with_non_str_rejected(self) -> None:
        with pytest.raises(ValueError, match="does not satisfy slot_type 'str_list'"):
            SlotSpec(slot_type="str_list", required=False, default=("a", 42))

    def test_required_slot_default_not_validated(self) -> None:
        # Required slots never use ``default``; the validator raises on
        # missing operator input. The default is irrelevant — keeping ``None``
        # as the sentinel for "no default" should not trip the guard.
        SlotSpec(slot_type="int", required=True, default=None)

    def test_optional_slot_default_none_skipped(self) -> None:
        # ``default=None`` is the sentinel for "no operator-visible default";
        # it bypasses validation regardless of slot_type.
        SlotSpec(slot_type="int", required=False, default=None)


# --------------------------------------------------------------------------
# Slot validation: type coercion + error messages
# --------------------------------------------------------------------------


class TestBlobIdSlotValidation:
    """The recipe boundary must reject URL-as-blob_id with a clear repair hint."""

    def test_valid_uuid_passes(self) -> None:
        bid = str(uuid4())
        result = apply_recipe(
            "classify-rows-llm-jsonl",
            {
                "source_blob_id": bid,
                "classifier_template": "Classify: {{ row['text'] }}",
                "profile": "tutorial",
            },
        )
        # blob_id is the top-level set_pipeline source argument (sibling of
        # options), NOT a key within options. set_pipeline forwards it to
        # _resolve_source_blob, which materialises options["path"] and the
        # canonical options["blob_ref"]. Putting blob_id inside options
        # bypasses resolution and leaves the source unbound.
        assert result["source"]["blob_id"] == bid
        assert "blob_id" not in result["source"]["options"]

    def test_classify_recipe_builds_public_profile_options_only(self) -> None:
        result = apply_recipe(
            "classify-rows-llm-jsonl",
            {
                "source_blob_id": str(uuid4()),
                "classifier_template": "Classify: {{ row['text'] }}",
                "profile": "tutorial",
            },
        )

        llm_options = result["nodes"][0]["options"]
        assert llm_options["profile"] == "tutorial"
        assert {"provider", "model", "api_key", "api_key_secret"}.isdisjoint(llm_options)

    def test_url_string_rejected_with_create_blob_hint(self) -> None:
        with pytest.raises(RecipeValidationError) as exc_info:
            apply_recipe(
                "classify-rows-llm-jsonl",
                {
                    "source_blob_id": "https://example.com/data.csv",
                    "classifier_template": "Classify: {{ row['text'] }}",
                    "profile": "tutorial-default",
                },
            )
        msg = str(exc_info.value)
        assert "valid UUID" in msg
        assert "create_blob" in msg
        assert "mime_type='text/plain'" in msg

    def test_path_string_rejected(self) -> None:
        with pytest.raises(RecipeValidationError, match="valid UUID"):
            apply_recipe(
                "classify-rows-llm-jsonl",
                {
                    "source_blob_id": "/tmp/data.csv",
                    "classifier_template": "Classify: {{ row['text'] }}",
                    "profile": "tutorial",
                },
            )

    def test_int_rejected(self) -> None:
        with pytest.raises(RecipeValidationError, match="UUID string"):
            apply_recipe(
                "classify-rows-llm-jsonl",
                {
                    "source_blob_id": 42,
                    "classifier_template": "Classify: {{ row['text'] }}",
                    "profile": "tutorial",
                },
            )


class TestNumericSlotValidation:
    def test_int_threshold_coerced_to_float(self) -> None:
        result = apply_recipe(
            "split-by-numeric-threshold",
            {
                "source_blob_id": str(uuid4()),
                "field": "price",
                "threshold": 100,
            },
        )
        # threshold is float in the gate condition
        gate_node = next(n for n in result["nodes"] if n["node_type"] == "gate")
        assert "100.0" in gate_node["condition"]

    def test_string_threshold_coerced_to_float(self) -> None:
        result = apply_recipe(
            "split-by-numeric-threshold",
            {
                "source_blob_id": str(uuid4()),
                "field": "price",
                "threshold": "99.95",
            },
        )
        gate_node = next(n for n in result["nodes"] if n["node_type"] == "gate")
        assert "99.95" in gate_node["condition"]

    def test_bool_rejected(self) -> None:
        """bool is a subclass of int in Python — reject explicitly."""
        with pytest.raises(RecipeValidationError, match="bool"):
            apply_recipe(
                "split-by-numeric-threshold",
                {
                    "source_blob_id": str(uuid4()),
                    "field": "price",
                    "threshold": True,
                },
            )

    def test_unparseable_string_rejected(self) -> None:
        with pytest.raises(RecipeValidationError, match="could not coerce"):
            apply_recipe(
                "split-by-numeric-threshold",
                {
                    "source_blob_id": str(uuid4()),
                    "field": "price",
                    "threshold": "not-a-number",
                },
            )


class TestRequiredOptionalSlots:
    def test_missing_required_slot_rejected(self) -> None:
        with pytest.raises(RecipeValidationError, match="missing required slot 'classifier_template'"):
            apply_recipe(
                "classify-rows-llm-jsonl",
                {
                    "source_blob_id": str(uuid4()),
                    "profile": "tutorial",
                },
            )

    def test_optional_slot_uses_default(self) -> None:
        result = apply_recipe(
            "classify-rows-llm-jsonl",
            {
                "source_blob_id": str(uuid4()),
                "classifier_template": "tmpl",
                "profile": "tutorial",
            },
        )
        llm_node = next(n for n in result["nodes"] if n["plugin"] == "llm")
        assert llm_node["options"]["profile"] == "tutorial"
        # label_field defaults to 'classification'
        assert llm_node["options"]["response_field"] == "classification"

    def test_missing_required_profile_rejected(self) -> None:
        with pytest.raises(RecipeValidationError, match="missing required slot 'profile'"):
            apply_recipe(
                "classify-rows-llm-jsonl",
                {
                    "source_blob_id": str(uuid4()),
                    "classifier_template": "tmpl",
                },
            )

    def test_unknown_slot_rejected(self) -> None:
        with pytest.raises(RecipeValidationError, match="does not accept slot"):
            apply_recipe(
                "classify-rows-llm-jsonl",
                {
                    "source_blob_id": str(uuid4()),
                    "classifier_template": "tmpl",
                    "profile": "tutorial",
                    "fictitious_slot": "x",
                },
            )


# --------------------------------------------------------------------------
# Recipe outputs — structural shape
# --------------------------------------------------------------------------


class TestClassifyRecipe:
    def _apply(self, **slots):
        defaults = {
            "source_blob_id": str(uuid4()),
            "classifier_template": "Classify {{ row['subject'] }}",
            "profile": "tutorial",
        }
        defaults.update(slots)
        return apply_recipe("classify-rows-llm-jsonl", defaults)

    def test_source_uses_blob_id_with_observed_schema(self) -> None:
        result = self._apply()
        src = result["source"]
        assert src["plugin"] == "csv"
        # blob_id is the top-level set_pipeline source argument (sibling of
        # options); set_pipeline forwards it to _resolve_source_blob.
        assert "blob_id" in src
        assert "blob_id" not in src["options"]
        assert src["options"]["schema"] == {"mode": "observed"}
        # discard default per the precursor commit
        assert src["on_validation_failure"] == "discard"

    def test_llm_node_wires_template_and_response_field(self) -> None:
        result = self._apply(label_field="urgency")
        llm = next(n for n in result["nodes"] if n["plugin"] == "llm")
        assert llm["options"]["prompt_template"] == "Classify {{ row['subject'] }}"
        assert llm["options"]["response_field"] == "urgency"

    def test_llm_options_satisfy_public_operator_profile_schema(self) -> None:
        result = self._apply()
        llm = next(node for node in result["nodes"] if node["plugin"] == "llm")
        settings = WebSettings(
            composer_max_composition_turns=4,
            composer_max_discovery_turns=4,
            composer_timeout_seconds=60,
            composer_rate_limit_per_minute=20,
            shareable_link_signing_key=b"0123456789abcdef0123456789abcdef",
            llm_profiles={
                "tutorial": {
                    "provider": "bedrock",
                    "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
                }
            },
            default_llm_profile="tutorial",
        )
        runtime = RuntimeWebPluginConfig.from_settings(settings)
        manager = _isolated_manager_with_llm_source()
        catalog = CatalogServiceImpl(manager)
        policy = compile_web_plugin_policy(registry=manager, settings=runtime)
        profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
        public_schema = profiles.public_schema(
            PluginId("transform", "llm"),
            catalog.get_schema("transform", "llm"),
            available_aliases=("tutorial",),
        ).json_schema

        errors = list(Draft202012Validator(public_schema).iter_errors(llm["options"]))

        assert errors == []
        assert llm["options"]["schema"] == {"mode": "observed", "fields": None}

    def test_output_is_jsonl(self) -> None:
        result = self._apply(output_path="outputs/tickets.jsonl")
        out = result["outputs"][0]
        assert out["plugin"] == "json"
        assert out["options"]["format"] == "jsonl"
        assert out["options"]["path"] == "outputs/tickets.jsonl"
        assert out["options"]["collision_policy"] == "auto_increment"

    def test_metadata_describes_recipe(self) -> None:
        result = self._apply()
        assert result["metadata"]["name"] == "classify-rows-llm-jsonl"


class TestThresholdRecipe:
    def _apply(self, **slots):
        defaults = {
            "source_blob_id": str(uuid4()),
            "field": "price",
            "threshold": 100.0,
        }
        defaults.update(slots)
        return apply_recipe("split-by-numeric-threshold", defaults)

    def test_type_coerce_node_targets_correct_field(self) -> None:
        result = self._apply(field="amount")
        coerce = next(n for n in result["nodes"] if n["plugin"] == "type_coerce")
        assert coerce["options"]["conversions"] == [{"field": "amount", "to": "float"}]

    def test_gate_condition_uses_field_and_threshold(self) -> None:
        result = self._apply(field="score", threshold=0.75)
        gate = next(n for n in result["nodes"] if n["node_type"] == "gate")
        assert gate["condition"] == "row['score'] >= 0.75"
        assert gate["routes"] == {"true": "above", "false": "below"}

    @pytest.mark.parametrize(
        "field",
        [
            "x'] is not None or row['x",
            "customer's score",
            'quote"and\\slash',
            "brackets[0]",
            "line\nbreak",
        ],
    )
    def test_gate_condition_treats_field_name_as_literal(self, field: str) -> None:
        result = self._apply(field=field, threshold=1.0)
        gate = next(n for n in result["nodes"] if n["node_type"] == "gate")

        condition = gate["condition"]
        assert condition == f"row[{field!r}] >= 1.0"

        parser = ExpressionParser(condition)
        assert parser.evaluate({field: 2.0, "x": 2.0}) is True
        assert parser.evaluate({field: 0.0, "x": 2.0}) is False

    def test_two_jsonl_outputs(self) -> None:
        result = self._apply(
            above_output_path="outputs/hi.jsonl",
            below_output_path="outputs/lo.jsonl",
        )
        outputs = {o["sink_name"]: o for o in result["outputs"]}
        assert set(outputs) == {"above", "below"}
        assert outputs["above"]["options"]["path"] == "outputs/hi.jsonl"
        assert outputs["below"]["options"]["path"] == "outputs/lo.jsonl"

    def test_chain_order(self) -> None:
        """Pipeline must run type_coerce BEFORE the gate."""
        result = self._apply()
        ids = [n["id"] for n in result["nodes"]]
        assert ids.index("coerce_numeric") < ids.index("threshold_gate")


class TestForkCoalesceTruncateRecipe:
    """Structural assertions for the fork-coalesce-truncate-jsonl recipe.

    The recipe encodes the gate.fork_to ↔ path.on_success ↔ coalesce.branches
    naming-correspondence invariants server-side so the LLM agent never has
    to maintain them. These tests pin those invariants — a refactor that
    rewires the connection names must keep all three lists self-consistent
    or these tests fail.
    """

    def _apply(self, **slots):
        defaults = {
            "source_blob_id": str(uuid4()),
            "truncate_field": "description",
            "max_chars": 30,
        }
        defaults.update(slots)
        return apply_recipe("fork-coalesce-truncate-jsonl", defaults)

    def test_source_uses_blob_id_with_observed_schema(self) -> None:
        result = self._apply()
        src = result["source"]
        assert src["plugin"] == "csv"
        assert "blob_id" in src
        # blob_id is the top-level set_pipeline source argument; if it ended
        # up nested in options the source would not be blob-bound and
        # _resolve_source_blob would not run (regression of Fix 4).
        assert "blob_id" not in src["options"]
        assert src["options"]["schema"] == {"mode": "observed"}
        assert src["on_validation_failure"] == "discard"

    def test_gate_carries_routes_all_fork_and_fork_to(self) -> None:
        """The validate_boolean_routes contract requires routes to be a
        non-empty dict; the canonical fork form is ``{"all": "fork"}`` plus
        a ``fork_to`` list naming the per-branch published connections.
        Both must be present together — either alone misroutes.

        ``fork_to`` entries are fixed internal branch connections. The
        coalesce mapping separately gives their results user-facing keys.
        """
        result = self._apply()
        gate = next(n for n in result["nodes"] if n["node_type"] == "gate")
        assert gate["condition"] == "'all'"
        assert gate["routes"] == {"all": "fork"}
        assert gate["fork_to"] == ["path_a", "path_b"]

    def test_path_a_passthrough_uses_fixed_internal_connections(self) -> None:
        """Path A's ``input`` must equal one of ``gate.fork_to`` entries
        and its ``on_success`` must publish the fixed connection consumed by
        ``coalesce.branches`` independently of the nested output key.
        """
        result = self._apply()
        path_a = next(n for n in result["nodes"] if n["id"] == "path_a_passthrough")
        assert path_a["plugin"] == "passthrough"
        assert path_a["input"] == "path_a"
        assert path_a["on_success"] == "path_a_out"
        assert path_a["options"]["schema"] == {"mode": "observed"}

    def test_path_b_truncate_uses_fixed_internal_connections(self) -> None:
        result = self._apply(truncate_field="notes", max_chars=42, truncation_suffix="…")
        path_b = next(n for n in result["nodes"] if n["id"] == "path_b_truncate")
        assert path_b["plugin"] == "truncate"
        assert path_b["input"] == "path_b"
        assert path_b["on_success"] == "path_b_out"
        assert path_b["options"]["fields"] == {"notes": 42}
        assert path_b["options"]["suffix"] == "…"
        assert path_b["options"]["schema"] == {"mode": "observed"}

    def test_coalesce_branches_mapping_uses_user_keys(self) -> None:
        """Mapping-form branches preserve output keys separately from
        the post-transform connections consumed by coalesce.
        """
        result = self._apply(key_a="original", key_b="truncated")
        coalesce = next(n for n in result["nodes"] if n["node_type"] == "coalesce")
        assert coalesce["branches"] == {"original": "path_a_out", "truncated": "path_b_out"}
        assert coalesce["policy"] == "require_all"
        assert coalesce["merge"] == "nested"
        assert coalesce["on_success"] == "merged_rows"
        # Coalesce nodes don't route by ``input`` (the producer-resolver
        # walks ``branches`` instead); the literal sentinel "branches" is
        # the established convention for the required-by-NodeSpec input.
        assert coalesce["input"] == "branches"

    def test_custom_keys_only_name_nested_output_fields(self) -> None:
        """Visible merge keys never become internal connection identities."""

        result = self._apply(key_a="raw", key_b="trimmed")
        gate = next(n for n in result["nodes"] if n["node_type"] == "gate")
        path_a = next(n for n in result["nodes"] if n["id"] == "path_a_passthrough")
        path_b = next(n for n in result["nodes"] if n["id"] == "path_b_truncate")
        coalesce = next(n for n in result["nodes"] if n["node_type"] == "coalesce")
        assert gate["fork_to"] == ["path_a", "path_b"]
        assert path_a["input"] == "path_a" and path_a["on_success"] == "path_a_out"
        assert path_b["input"] == "path_b" and path_b["on_success"] == "path_b_out"
        assert coalesce["branches"] == {"raw": "path_a_out", "trimmed": "path_b_out"}

    @pytest.mark.parametrize(
        ("key_a", "key_b"),
        [
            ("rows", "a_out"),
            ("merged_rows", "custom"),
            ("custom", "merged_rows"),
        ],
    )
    def test_visible_keys_cannot_collide_with_internal_connections(self, key_a: str, key_b: str) -> None:
        result = self._apply(key_a=key_a, key_b=key_b)
        gate = next(n for n in result["nodes"] if n["node_type"] == "gate")
        path_a = next(n for n in result["nodes"] if n["id"] == "path_a_passthrough")
        path_b = next(n for n in result["nodes"] if n["id"] == "path_b_truncate")
        coalesce = next(n for n in result["nodes"] if n["node_type"] == "coalesce")

        assert gate["fork_to"] == ["path_a", "path_b"]
        assert (path_a["input"], path_a["on_success"]) == ("path_a", "path_a_out")
        assert (path_b["input"], path_b["on_success"]) == ("path_b", "path_b_out")
        assert coalesce["branches"] == {key_a: "path_a_out", key_b: "path_b_out"}

    def test_equal_visible_keys_are_rejected_before_building(self) -> None:
        with pytest.raises(RecipeValidationError, match="distinct"):
            self._apply(key_a="same", key_b="same")

    def test_default_keys_match_scenario_request(self) -> None:
        """Scenario fork_and_coalesce asks for keys 'path_a' and 'path_b';
        the recipe defaults must match so an LLM that omits the key slots
        still produces the requested shape.
        """
        result = self._apply()
        coalesce = next(n for n in result["nodes"] if n["node_type"] == "coalesce")
        assert list(coalesce["branches"]) == ["path_a", "path_b"]

    def test_default_truncation_suffix_is_ellipsis(self) -> None:
        result = self._apply()
        path_b = next(n for n in result["nodes"] if n["id"] == "path_b_truncate")
        assert path_b["options"]["suffix"] == "..."

    def test_single_jsonl_output_consumes_merged_rows(self) -> None:
        result = self._apply(output_path="outputs/custom.jsonl")
        outputs = result["outputs"]
        assert len(outputs) == 1
        out = outputs[0]
        # sink_name must equal coalesce.on_success — otherwise no producer
        # for this sink and the runtime rejects the pipeline.
        assert out["sink_name"] == "merged_rows"
        assert out["plugin"] == "json"
        assert out["options"]["format"] == "jsonl"
        assert out["options"]["path"] == "outputs/custom.jsonl"

    def test_node_kinds_include_both_gate_and_coalesce(self) -> None:
        """The fork-and-coalesce scenario's green criterion requires both
        kinds present in the converged state; the recipe must satisfy it
        unconditionally regardless of slot values.
        """
        result = self._apply()
        kinds = {n["node_type"] for n in result["nodes"]}
        assert "gate" in kinds
        assert "coalesce" in kinds

    def test_metadata_describes_recipe(self) -> None:
        result = self._apply()
        assert result["metadata"]["name"] == "fork-coalesce-truncate-jsonl"

    def test_max_chars_bool_rejected(self) -> None:
        """bool is a subclass of int in Python — the slot validator rejects
        it explicitly so a typo doesn't silently set max_chars to 0 or 1.
        """
        with pytest.raises(RecipeValidationError, match="bool"):
            apply_recipe(
                "fork-coalesce-truncate-jsonl",
                {
                    "source_blob_id": str(uuid4()),
                    "truncate_field": "description",
                    "max_chars": True,
                },
            )

    def test_missing_required_truncate_field_rejected(self) -> None:
        with pytest.raises(RecipeValidationError, match="missing required slot 'truncate_field'"):
            apply_recipe(
                "fork-coalesce-truncate-jsonl",
                {
                    "source_blob_id": str(uuid4()),
                    "max_chars": 30,
                },
            )

    def test_url_blob_id_rejected_with_create_blob_hint(self) -> None:
        """Same blob_id slot semantics as the other recipes — URLs are
        rejected with a hint pointing at create_blob.
        """
        with pytest.raises(RecipeValidationError) as exc_info:
            apply_recipe(
                "fork-coalesce-truncate-jsonl",
                {
                    "source_blob_id": "https://example.com/customers.csv",
                    "truncate_field": "description",
                    "max_chars": 30,
                },
            )
        assert "create_blob" in str(exc_info.value)


class TestWebScrapeRecipeBuild:
    """The web-scrape recipe deterministically emits
    source → web_scrape → llm → field_mapper(cleanup) → jsonl."""

    _SLOTS: ClassVar[dict[str, str]] = {
        "source_blob_id": str(uuid4()),
        "source_plugin": "json",
        "profile": "tutorial-default",
        "abuse_contact": "web-scrape-contact@dta.gov.au",
        "scraping_reason": "Tutorial exercise: fetch public pages for rating",
        "output_path": "outputs/ratings.jsonl",
    }

    def _build(self) -> dict:
        return apply_recipe("web-scrape-llm-rate-jsonl", self._SLOTS)

    def test_head_source_node_uses_resolved_source_plugin(self) -> None:
        args = self._build()
        assert args["source"]["plugin"] == "json"
        assert args["source"]["blob_id"] == self._SLOTS["source_blob_id"]
        assert args["source"]["on_success"] == "rows"

    def test_csv_url_source_stays_csv(self) -> None:
        slots = {**self._SLOTS, "source_plugin": "csv"}
        args = apply_recipe("web-scrape-llm-rate-jsonl", slots)
        assert args["source"]["plugin"] == "csv"
        assert args["source"]["blob_id"] == slots["source_blob_id"]

    def test_canonical_chain_order(self) -> None:
        args = self._build()
        plugins = [n["plugin"] for n in args["nodes"]]
        assert plugins == ["web_scrape", "llm", "field_mapper"]

    def test_chain_is_wired_by_connection_labels(self) -> None:
        args = self._build()
        by_plugin = {n["plugin"]: n for n in args["nodes"]}
        assert by_plugin["web_scrape"]["input"] == "rows"
        assert by_plugin["web_scrape"]["on_success"] == "scraped"
        assert by_plugin["llm"]["input"] == "scraped"
        assert by_plugin["llm"]["on_success"] == "rated"
        assert by_plugin["field_mapper"]["input"] == "rated"
        assert by_plugin["field_mapper"]["on_success"] == "clean"
        assert args["outputs"][0]["sink_name"] == "clean"
        assert args["outputs"][0]["plugin"] == "json"
        assert args["outputs"][0]["options"]["format"] == "jsonl"

    def test_field_mapper_select_only_excludes_raw_content_and_fingerprint(self) -> None:
        """Data-minimization: the cleanup sink field set EXCLUDES the raw
        web_scrape content/fingerprint fields (pin the actual output set)."""
        args = self._build()
        fm = next(n for n in args["nodes"] if n["plugin"] == "field_mapper")
        assert fm["options"]["select_only"] is True
        mapping = fm["options"]["mapping"]
        preserved = set(mapping) | set(mapping.values())
        assert "content" not in preserved
        assert "content_fingerprint" not in preserved
        # Positive pin: the rating + url ARE preserved (the user-facing output).
        assert "rating" in preserved
        assert "url" in preserved

    def test_field_mapper_stages_pipeline_decision_cleanup_requirement(self) -> None:
        """The raw-HTML cleanup pipeline_decision is staged on the field_mapper
        node so the blocking cleanup contract passes (tools/sessions.py:670 →
        raw_html_cleanup_review_contract_error)."""
        from elspeth.web.interpretation_state import (
            INTERPRETATION_REQUIREMENTS_KEY,
            RAW_HTML_CLEANUP_REVIEW_DRAFT,
            RAW_HTML_CLEANUP_USER_TERM,
        )

        args = self._build()
        fm = next(n for n in args["nodes"] if n["plugin"] == "field_mapper")
        reqs = fm["options"][INTERPRETATION_REQUIREMENTS_KEY]
        decision = next(r for r in reqs if r["kind"] == "pipeline_decision")
        assert decision == {
            "kind": "pipeline_decision",
            "user_term": RAW_HTML_CLEANUP_USER_TERM,
            "draft": RAW_HTML_CLEANUP_REVIEW_DRAFT,
        }

    def test_web_scrape_node_declares_content_and_fingerprint_fields(self) -> None:
        """web_scrape must name content_field/fingerprint_field so
        _web_scrape_raw_fields can compute the raw set the cleanup drops."""
        args = self._build()
        ws = next(n for n in args["nodes"] if n["plugin"] == "web_scrape")
        assert ws["options"]["url_field"] == "url"
        assert ws["options"]["content_field"] == "content"
        assert ws["options"]["fingerprint_field"] == "content_fingerprint"

    def test_http_identity_options_are_operator_supplied_slots(self) -> None:
        args = self._build()
        ws = next(n for n in args["nodes"] if n["plugin"] == "web_scrape")
        assert ws["options"]["http"]["abuse_contact"] == self._SLOTS["abuse_contact"]
        assert ws["options"]["http"]["scraping_reason"] == self._SLOTS["scraping_reason"]

    def test_no_azure_prompt_shield_hard_node(self) -> None:
        """rev 4: omit the unbuildable azure_prompt_shield hard node
        (elspeth-abb2cb0931 — composer cannot instantiate it without
        configured endpoint+api_key secrets)."""
        args = self._build()
        plugins = {n["plugin"] for n in args["nodes"]}
        assert "azure_prompt_shield" not in plugins


# --------------------------------------------------------------------------
# Unknown recipe + edge cases
# --------------------------------------------------------------------------


class TestUnknownRecipe:
    def test_apply_unknown_raises(self) -> None:
        with pytest.raises(RecipeValidationError, match="not registered"):
            apply_recipe("imaginary-recipe", {})

    def test_error_lists_available_recipes(self) -> None:
        with pytest.raises(RecipeValidationError) as exc_info:
            apply_recipe("imaginary-recipe", {})
        msg = str(exc_info.value)
        assert "classify-rows-llm-jsonl" in msg
        assert "split-by-numeric-threshold" in msg


# --------------------------------------------------------------------------
# End-to-end: recipe args must flow through set_pipeline so the source is
# blob-bound (options["blob_ref"] populated). compute_proof_diagnostics
# reads options["blob_ref"]; if recipes write blob_id in the wrong location
# it silently bypasses _resolve_source_blob and proof_diagnostics returns
# empty. This is the regression Fix 4 addresses.
# --------------------------------------------------------------------------


class TestRecipeIntegrationWithSetPipeline:
    """Apply a recipe through ``execute_tool('apply_pipeline_recipe', ...)``
    and confirm the resulting source carries the canonical ``blob_ref``
    key — without it, the proof step silently sees no source.
    """

    @pytest.fixture
    def _seeded_blob(self, tmp_path):
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(engine)
        session_id = str(uuid4())
        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test",
                    created_at=now,
                    updated_at=now,
                )
            )

        blob_id = str(uuid4())
        storage_dir = tmp_path / "blobs" / session_id
        storage_dir.mkdir(parents=True)
        storage_path = storage_dir / f"{blob_id}_orders.csv"
        body = b"order_id,customer,price\nO-1,Alice,49.95\n"
        storage_path.write_bytes(body)
        with engine.begin() as conn:
            conn.execute(
                blobs_table.insert().values(
                    id=blob_id,
                    session_id=session_id,
                    filename="orders.csv",
                    mime_type="text/csv",
                    size_bytes=len(body),
                    content_hash=_content_hash(body),
                    storage_path=str(storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )
        return engine, session_id, blob_id

    def _context(self):
        # Use the real PluginManager so set_pipeline's prevalidation can
        # see authentic schemas for csv/llm/type_coerce/json. Mocking
        # list_*/get_schema would force us to fabricate schemas — the
        # real catalog is cheap (builtin registration only) and avoids
        # masking schema mismatches.
        from elspeth.plugins.infrastructure.manager import PluginManager
        from elspeth.web.catalog.policy_view import PolicyCatalogView
        from elspeth.web.catalog.service import CatalogServiceImpl
        from elspeth.web.composer.tools._common import ToolContext

        pm = PluginManager()
        pm.register_builtin_plugins()
        catalog = CatalogServiceImpl(pm)
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
        return ToolContext(
            catalog=PolicyCatalogView.for_trained_operator(catalog, snapshot),
            plugin_snapshot=snapshot,
        )

    def test_classify_recipe_blob_id_at_top_level(self) -> None:
        """Recipe places blob_id at source top-level so set_pipeline's
        ``src_args.get('blob_id')`` finds it and invokes _resolve_source_blob.

        Regression test: previously blob_id was nested in source.options,
        which silently bypassed resolution and left the source with
        ``options['blob_id']`` (an unknown key) instead of the canonical
        ``options['blob_ref']``. compute_proof_diagnostics reads blob_ref,
        so the proof step silently saw zero source-bound diagnostics.
        """
        bid = str(uuid4())
        result = apply_recipe(
            "classify-rows-llm-jsonl",
            {
                "source_blob_id": bid,
                "classifier_template": "tmpl",
                "profile": "tutorial",
            },
        )
        # Top-level — set_pipeline consumes this in src_args.get('blob_id').
        assert result["source"]["blob_id"] == bid
        # Must NOT be in options; if it were, set_pipeline would not see
        # it and _resolve_source_blob would not be invoked.
        assert "blob_id" not in result["source"]["options"]

    def test_threshold_recipe_blob_id_at_top_level(self) -> None:
        bid = str(uuid4())
        result = apply_recipe(
            "split-by-numeric-threshold",
            {
                "source_blob_id": bid,
                "field": "price",
                "threshold": 50.0,
            },
        )
        assert result["source"]["blob_id"] == bid
        assert "blob_id" not in result["source"]["options"]

    def test_threshold_source_resolves_to_blob_ref_via_resolve_source_blob(self, _seeded_blob, tmp_path) -> None:
        """End-to-end on the source segment only (the bug surface):
        feeding the recipe's ``source.blob_id`` + ``source.options`` into
        ``_resolve_source_blob`` (the function set_pipeline invokes for
        blob-bound sources) yields ``options['blob_ref']`` and the
        canonical storage path. compute_proof_diagnostics reads blob_ref,
        so this is the load-bearing invariant.

        This narrows scope to the bug surface (key mismatch). The full
        recipe DAG has separate, pre-existing prevalidation gaps for the
        llm/type_coerce transforms (unrelated to Fix 4) which would
        otherwise reject set_pipeline; testing the source segment in
        isolation avoids coupling Fix 4 to those.
        """
        from elspeth.web.composer.state import CompositionState, PipelineMetadata
        from elspeth.web.composer.tools import _resolve_source_blob

        engine, session_id, blob_id = _seeded_blob
        empty = CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        # Build the recipe args, then exercise _resolve_source_blob with
        # the exact shape set_pipeline would feed it.
        args = apply_recipe(
            "split-by-numeric-threshold",
            {"source_blob_id": blob_id, "field": "price", "threshold": 50.0},
        )
        src_args = args["source"]

        resolved = _resolve_source_blob(
            blob_id=src_args["blob_id"],
            explicit_plugin=src_args["plugin"],
            caller_options=src_args["options"],
            on_validation_failure=src_args["on_validation_failure"],
            state=empty,
            context=self._context(),
            session_engine=engine,
            session_id=session_id,
        )

        # _resolve_source_blob returns a _ResolvedSourceBlob on success,
        # or a ToolResult on failure — type-discriminate.
        from elspeth.web.composer.tools import _ResolvedSourceBlob

        assert isinstance(resolved, _ResolvedSourceBlob), getattr(resolved, "data", resolved)
        # Canonical key set by _resolve_source_blob.
        assert resolved.options["blob_ref"] == blob_id
        # Storage path should resolve to the seeded file.
        assert "path" in resolved.options
        # The recipe's caller_options pass through (schema preserved).
        assert resolved.options["schema"] == {"mode": "observed"}


# --------------------------------------------------------------------------
# End-to-end: apply_pipeline_recipe MCP tool invocation drives the full
# chain through set_pipeline's prevalidation, then compute_proof_diagnostics
# reads the resulting state's source.options["blob_ref"]. This is the
# tier-up of Fix 4 (PARTIAL → VERIFIED): the previous coverage stopped at
# _resolve_source_blob in isolation because both recipes generated
# transform options that failed schema prevalidation downstream
# (filigree-obs-2344c737a6). With the recipes now emitting schema-valid
# options for llm and type_coerce, apply_pipeline_recipe can flow through
# set_pipeline successfully and the proof step can be exercised end-to-end.
# --------------------------------------------------------------------------


class TestApplyRecipeEndToEnd:
    """Drive each recipe through ``execute_tool('apply_pipeline_recipe', ...)``
    and confirm (a) the recipe's transform options now satisfy plugin
    schema prevalidation (no more "missing schema/api_key" failures) and
    (b) ``compute_proof_diagnostics`` reads the resulting state's
    ``source.options['blob_ref']`` end-to-end without error.
    """

    @pytest.fixture
    def _seeded(self, tmp_path):
        from datetime import UTC, datetime

        from sqlalchemy.pool import StaticPool

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(engine)
        session_id = str(uuid4())
        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test",
                    created_at=now,
                    updated_at=now,
                )
            )

        blob_id = str(uuid4())
        storage_dir = tmp_path / "blobs" / session_id
        storage_dir.mkdir(parents=True)
        storage_path = storage_dir / f"{blob_id}_orders.csv"
        body = b"order_id,customer,price\nO-1,Alice,49.95\nO-2,Bob,150.00\n"
        storage_path.write_bytes(body)
        with engine.begin() as conn:
            conn.execute(
                blobs_table.insert().values(
                    id=blob_id,
                    session_id=session_id,
                    filename="orders.csv",
                    mime_type="text/csv",
                    size_bytes=len(body),
                    content_hash=_content_hash(body),
                    storage_path=str(storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )
        return engine, session_id, blob_id, tmp_path

    def _policy_context(self):
        # Real PluginManager so set_pipeline's prevalidation sees authentic
        # schemas for csv/llm/type_coerce/json. Mocking the catalog would
        # mask the schema-validity contract under test here.
        from elspeth.web.catalog.policy_view import PolicyCatalogView
        from elspeth.web.config import WebSettings
        from elspeth.web.plugin_policy.availability import build_plugin_snapshot
        from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
        from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig

        pm = _isolated_manager_with_llm_source()
        catalog = CatalogServiceImpl(pm)
        settings = WebSettings(
            composer_max_composition_turns=4,
            composer_max_discovery_turns=4,
            composer_timeout_seconds=60,
            composer_rate_limit_per_minute=20,
            shareable_link_signing_key=b"0123456789abcdef0123456789abcdef",
            plugin_allowlist=(
                "transform:passthrough",
                "transform:truncate",
                "transform:type_coerce",
            ),
            llm_profiles={
                "tutorial-default": {
                    "provider": "bedrock",
                    "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
                }
            },
            default_llm_profile="tutorial-default",
        )
        runtime = RuntimeWebPluginConfig.from_settings(settings)
        policy = compile_web_plugin_policy(registry=pm, settings=runtime)
        profiles = OperatorProfileRegistry(policy=policy, settings=runtime)

        class _NoSecrets:
            def has_server_ref(self, name: str) -> bool:
                return False

            def has_user_ref(self, principal: str, name: str) -> bool:
                return False

            def has_ref(self, principal: str, name: str) -> bool:
                return False

        snapshot = build_plugin_snapshot(
            policy=policy,
            catalog=catalog,
            profiles=profiles,
            principal_scope="local:test-user",
            secret_inventory=_NoSecrets(),
            generation_key=b"classify-recipe-policy-key",
        )
        return PolicyCatalogView(catalog, snapshot, profiles), snapshot

    def test_classify_recipe_passes_set_pipeline_prevalidation_and_drives_proof_step(self, _seeded) -> None:
        """The classify recipe must:

        1. Pass ``_execute_set_pipeline`` prevalidation (the bug surface —
           previously failed with "schema required" / "api_key required"
           on the llm node). Verified by ``result.success``.
        2. Produce a state whose ``source.options['blob_ref']`` matches
           the seeded blob — proves ``_resolve_source_blob`` ran and the
           canonical key was written.
        3. Allow ``compute_proof_diagnostics`` to walk the resulting
           state without raising. The observed-mode blob has no missing
           columns so we expect zero diagnostics, but the call must
           successfully reach the blob read (proving the wiring closed
           the gap that previously made proof_diagnostics return [] by
           never finding ``blob_ref``).
        """
        from elspeth.web.composer.state import CompositionState, PipelineMetadata
        from elspeth.web.composer.tools import compute_proof_diagnostics, execute_tool

        engine, session_id, blob_id, _data_dir = _seeded
        empty = CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        catalog, snapshot = self._policy_context()
        result = execute_tool(
            "apply_pipeline_recipe",
            {
                "recipe_name": "classify-rows-llm-jsonl",
                "slots": {
                    "source_blob_id": blob_id,
                    "classifier_template": "Classify: {{ row['customer'] }}",
                    "profile": "tutorial-default",
                    # Declare the field referenced in classifier_template
                    # explicitly. The LLMConfig validator demands an
                    # explicit list when the template references row.*;
                    # supplying [customer] proves the slot drives the
                    # generated config end-to-end.
                    "required_input_fields": ["customer"],
                },
            },
            empty,
            catalog,
            plugin_snapshot=snapshot,
            session_engine=engine,
            session_id=session_id,
        )
        # 1. set_pipeline prevalidation passed — this is the bug surface.
        assert result.success, getattr(result, "data", result)

        # 2. Source is now blob-bound.
        new_state = result.updated_state
        assert new_state.sources["source"].options["blob_ref"] == blob_id

        # 3. proof_diagnostics walks the state without raising and
        #    reaches the blob read (observable via the diagnostics list
        #    being a list — not None or an exception).
        diagnostics = compute_proof_diagnostics(
            new_state,
            session_engine=engine,
            session_id=session_id,
        )
        assert isinstance(diagnostics, list)
        # Observed schema + headers in the blob: no blocking diagnostics.
        # Any non-info diagnostic would be a real signal worth surfacing.
        assert all(d["severity"] != "blocking" for d in diagnostics), diagnostics

        # Recipe output stays within the public operator-profile schema.
        classifier = next(n for n in new_state.nodes if n.plugin == "llm")
        assert classifier.options["profile"] == "tutorial-default"
        assert {"provider", "model", "api_key", "api_key_secret"}.isdisjoint(classifier.options)
        # Schema option flows through prevalidation and is durable.
        assert classifier.options["schema"] == {"mode": "observed", "fields": None}

    def test_threshold_recipe_passes_set_pipeline_prevalidation_and_drives_proof_step(self, _seeded) -> None:
        """The threshold recipe must:

        1. Pass ``_execute_set_pipeline`` prevalidation (the bug surface —
           previously failed with "schema required" on the type_coerce node).
        2. Produce a state whose ``source.options['blob_ref']`` matches
           the seeded blob.
        3. Drive ``compute_proof_diagnostics`` end-to-end through the
           full apply_pipeline_recipe → set_pipeline → state mutation
           chain rather than the previous in-isolation
           ``_resolve_source_blob`` call.
        """
        from elspeth.web.composer.state import CompositionState, PipelineMetadata
        from elspeth.web.composer.tools import compute_proof_diagnostics, execute_tool

        engine, session_id, blob_id, _data_dir = _seeded
        empty = CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        catalog, snapshot = self._policy_context()
        result = execute_tool(
            "apply_pipeline_recipe",
            {
                "recipe_name": "split-by-numeric-threshold",
                "slots": {
                    "source_blob_id": blob_id,
                    "field": "price",
                    "threshold": 100.0,
                },
            },
            empty,
            catalog,
            plugin_snapshot=snapshot,
            session_engine=engine,
            session_id=session_id,
        )
        assert result.success, getattr(result, "data", result)

        new_state = result.updated_state
        assert new_state.sources["source"].options["blob_ref"] == blob_id

        diagnostics = compute_proof_diagnostics(
            new_state,
            session_engine=engine,
            session_id=session_id,
        )
        assert isinstance(diagnostics, list)
        assert all(d["severity"] != "blocking" for d in diagnostics), diagnostics

        # type_coerce now carries the required schema option through
        # prevalidation into the durable state.
        coerce = next(n for n in new_state.nodes if n.plugin == "type_coerce")
        assert coerce.options["schema"] == {"mode": "observed"}
        assert coerce.options["conversions"] == ({"field": "price", "to": "float"},) or coerce.options["conversions"] == [
            {"field": "price", "to": "float"}
        ]

    def test_fork_coalesce_recipe_passes_runtime_equivalent_validation(self, _seeded) -> None:
        """The fork/coalesce recipe must survive the runtime DAG builder.

        This pins the live RGR failure where composer Stage 1 accepted
        list-form coalesce branches, but runtime rejected transformed fork
        branches because ``gate.fork_to`` branch names were not present as
        coalesce branch identities.
        """
        from types import SimpleNamespace

        from elspeth.web.composer import yaml_generator
        from elspeth.web.composer.state import CompositionState, PipelineMetadata
        from elspeth.web.composer.tools import execute_tool
        from elspeth.web.execution.validation import validate_pipeline_for_trained_operator

        engine, session_id, blob_id, data_dir = _seeded
        empty = CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        catalog, snapshot = self._policy_context()
        result = execute_tool(
            "apply_pipeline_recipe",
            {
                "recipe_name": "fork-coalesce-truncate-jsonl",
                "slots": {
                    "source_blob_id": blob_id,
                    "truncate_field": "customer",
                    "max_chars": 5,
                    "output_path": "outputs/merged.jsonl",
                    "key_a": "path_a",
                    "key_b": "path_b",
                },
            },
            empty,
            catalog,
            plugin_snapshot=snapshot,
            session_engine=engine,
            session_id=session_id,
        )
        assert result.success, getattr(result, "data", result)
        assert result.updated_state.validate().is_valid is True

        runtime = validate_pipeline_for_trained_operator(
            result.updated_state,
            SimpleNamespace(data_dir=data_dir),
            yaml_generator,
            session_id=session_id,
        )
        assert runtime.is_valid is True, runtime.errors

    def test_apply_recipe_over_populated_state_emits_replacement_note(self, _seeded) -> None:
        """When the operator has hand-iterated state and the LLM applies a
        recipe, the destructive full-state replacement must be visible.

        Regression for the silent-overwrite issue: ``apply_pipeline_recipe``
        delegates to ``set_pipeline`` (full-state replacement). If the
        prior state had a source / nodes / outputs, the LLM (and the
        audit trail) should see a non-blocking note describing what was
        replaced. This preserves the recipe-on-existing-state ergonomics
        — we don't refuse the destructive call — but the action is no
        longer silent.
        """
        from elspeth.web.composer.state import (
            CompositionState,
            NodeSpec,
            OutputSpec,
            PipelineMetadata,
            SourceSpec,
        )
        from elspeth.web.composer.tools import execute_tool

        engine, session_id, blob_id, _data_dir = _seeded

        # Prior state: a hand-iterated pipeline the operator was building
        # (source set, two transforms, one output). Applying a recipe on
        # top of this is the silent-overwrite scenario.
        populated = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="hand_built_a",
                options={"path": "old-input.csv"},
                on_validation_failure="discard",
            ),
            nodes=(
                NodeSpec(
                    id="hand_built_a",
                    node_type="transform",
                    plugin="select_columns",
                    input="source",
                    on_success="hand_built_b",
                    on_error=None,
                    options={"fields": ["a", "b"]},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
                NodeSpec(
                    id="hand_built_b",
                    node_type="transform",
                    plugin="select_columns",
                    input="hand_built_a",
                    on_success="prior_output",
                    on_error=None,
                    options={"fields": ["a"]},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            edges=(),
            outputs=(
                OutputSpec(
                    name="prior_output",
                    plugin="json",
                    options={"path": "old-output.json"},
                    on_write_failure="quarantine",
                ),
            ),
            metadata=PipelineMetadata(),
            version=4,
        )

        catalog, snapshot = self._policy_context()
        result = execute_tool(
            "apply_pipeline_recipe",
            {
                "recipe_name": "split-by-numeric-threshold",
                "slots": {
                    "source_blob_id": blob_id,
                    "field": "price",
                    "threshold": 100.0,
                },
            },
            populated,
            catalog,
            plugin_snapshot=snapshot,
            session_engine=engine,
            session_id=session_id,
        )

        assert result.success, getattr(result, "data", result)
        # The destructive replacement is now audible: data carries a
        # ``replaced_pipeline_note`` describing the prior counts. ToolResult
        # post-init deep-freezes the data field, so the type at this point
        # is a MappingProxyType wrapping the merged dict — accept any
        # ``Mapping`` rather than asserting ``dict``.
        from collections.abc import Mapping as _Mapping

        assert isinstance(result.data, _Mapping)
        note = result.data["replaced_pipeline_note"]
        assert "source=set" in note
        assert "2 node(s)" in note
        assert "1 output(s)" in note
        # Pre-existing data payload (from set_pipeline's source_blob
        # resolution) survives the merge — we did not clobber it.
        assert "source_blob" in result.data

    def test_fork_coalesce_truncate_recipe_passes_set_pipeline_prevalidation(self, _seeded) -> None:
        """The fork-coalesce-truncate recipe must:

        1. Pass ``_execute_set_pipeline`` prevalidation end-to-end. This is
           the load-bearing test — if the gate.fork_to ↔ path.input/on_success
           ↔ coalesce.branches naming has any inconsistency, the validator
           catches it here. Verified by ``result.success``.
        2. Produce a state whose ``source.options['blob_ref']`` matches the
           seeded blob — proves blob resolution ran.
        3. Yield a node-kinds set that includes both ``gate`` and ``coalesce``,
           which is the green criterion of the fork-and-coalesce scenario.
        4. Yield a single output sink consuming ``merged_rows`` (the
           coalesce's published connection).

        The cheap composer model's historical failure mode was authoring a
        gate+2-sink degraded shape (no coalesce) — this test pins that the
        recipe never produces that, regardless of which optional slots the
        agent supplies.
        """
        from elspeth.web.composer.state import CompositionState, PipelineMetadata
        from elspeth.web.composer.tools import compute_proof_diagnostics, execute_tool

        engine, session_id, blob_id, _data_dir = _seeded
        empty = CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        catalog, snapshot = self._policy_context()
        result = execute_tool(
            "apply_pipeline_recipe",
            {
                "recipe_name": "fork-coalesce-truncate-jsonl",
                "slots": {
                    "source_blob_id": blob_id,
                    "truncate_field": "customer",
                    "max_chars": 5,
                },
            },
            empty,
            catalog,
            plugin_snapshot=snapshot,
            session_engine=engine,
            session_id=session_id,
        )
        # 1. set_pipeline prevalidation passed — this is the bug surface.
        #    A wiring error in gate.fork_to / path.input / coalesce.branches
        #    naming would surface here as an unresolved-connection error.
        assert result.success, getattr(result, "data", result)

        # 2. Source is now blob-bound.
        new_state = result.updated_state
        assert new_state.sources["source"].options["blob_ref"] == blob_id

        # 3. Both gate AND coalesce are present (scenario green criterion
        #    is must_have_node_kinds_substring_any_of=[["gate", "coalesce"]]).
        kinds = {n.node_type for n in new_state.nodes}
        assert "gate" in kinds, kinds
        assert "coalesce" in kinds, kinds

        # 4. Single output sink, jsonl format, consuming the coalesce output.
        assert len(new_state.outputs) == 1
        out = new_state.outputs[0]
        assert out.name == "merged_rows"

        # proof_diagnostics walks the state without raising and reaches the
        # blob read. Headers in the seeded blob are 'order_id,customer,price'
        # so 'customer' (the truncate_field) exists — no blocking diagnostics.
        diagnostics = compute_proof_diagnostics(
            new_state,
            session_engine=engine,
            session_id=session_id,
        )
        assert isinstance(diagnostics, list)
        assert all(d["severity"] != "blocking" for d in diagnostics), diagnostics

    def test_apply_recipe_over_empty_state_emits_no_replacement_note(self, _seeded) -> None:
        """Recipe applied to a fresh session is not destructive — no note."""
        from elspeth.web.composer.state import CompositionState, PipelineMetadata
        from elspeth.web.composer.tools import execute_tool

        engine, session_id, blob_id, _data_dir = _seeded
        empty = CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        catalog, snapshot = self._policy_context()
        result = execute_tool(
            "apply_pipeline_recipe",
            {
                "recipe_name": "split-by-numeric-threshold",
                "slots": {
                    "source_blob_id": blob_id,
                    "field": "price",
                    "threshold": 100.0,
                },
            },
            empty,
            catalog,
            plugin_snapshot=snapshot,
            session_engine=engine,
            session_id=session_id,
        )
        assert result.success, getattr(result, "data", result)
        # No replacement note when there was nothing to replace.
        if result.data is not None:
            assert "replaced_pipeline_note" not in result.data


# --------------------------------------------------------------------------
# Deep-freeze invariants on RecipeSpec / SlotSpec
#
# RecipeSpec.__post_init__ calls ``freeze_fields(self, "slots")`` so the
# slot mapping cannot be mutated after construction. These tests pin that
# contract so a future refactor that swaps freeze_fields for a forbidden
# pattern (e.g. ``MappingProxyType(self.slots)`` — a CLAUDE.md-banned
# shallow wrap that leaves the original dict mutable through the wrapped
# reference) would fail loudly here rather than silently regress recipe
# integrity. Mirrors the pattern in
# tests/unit/web/composer/test_source_inspection.py::TestFrozenInvariants.
# --------------------------------------------------------------------------


class TestRecipeSpecFrozenInvariants:
    """RecipeSpec.slots must be deeply immutable post-construction.

    See ``deep_freeze`` (src/elspeth/contracts/freeze.py): ``Mapping``
    inputs are converted to a fresh ``MappingProxyType`` over a freshly
    materialised dict — both the wrapper and the underlying dict are
    detached from the caller's input, so external mutation of the input
    cannot leak through to ``recipe.slots``. SlotSpec values themselves
    are frozen dataclasses (``frozen=True, slots=True``).
    """

    def test_slots_item_assignment_new_key_raises_typeerror(self) -> None:
        """Item assignment on a registered recipe's slots is rejected.

        ``MappingProxyType`` raises ``TypeError`` on ``__setitem__``; this
        is the load-bearing barrier that prevents mutation of a recipe's
        slot table after construction.
        """
        spec = get_recipe("classify-rows-llm-jsonl")
        assert spec is not None
        with pytest.raises(TypeError):
            spec.slots["new_slot"] = SlotSpec(slot_type="str")  # type: ignore[index]

    def test_slots_item_assignment_existing_key_raises_typeerror(self) -> None:
        """Reassigning an existing slot is also rejected.

        Distinct from the new-key case: a hypothetical regression that
        wrapped ``self.slots`` in a ``MappingProxyType`` view (without
        the deep-freeze detach) would still raise ``TypeError`` on the
        proxy, but the underlying dict would remain mutable through the
        original reference. This test pins the proxy-level barrier.
        """
        spec = get_recipe("split-by-numeric-threshold")
        assert spec is not None
        # 'field' is a known slot of the threshold recipe.
        assert "field" in spec.slots
        with pytest.raises(TypeError):
            spec.slots["field"] = SlotSpec(slot_type="str")  # type: ignore[index]

    def test_external_dict_mutation_does_not_leak_into_slots(self) -> None:
        """Mutating the dict passed into ``RecipeSpec`` must not affect ``recipe.slots``.

        This is the discriminating regression test against a forbidden
        ``MappingProxyType(self.slots)`` shallow-wrap. ``deep_freeze``
        materialises a fresh dict from the input's items before wrapping,
        so post-construction mutations to the original dict are isolated.
        A shallow-wrap regression with a ``dict`` input would expose the
        leak — the proxy would be a *view* onto the still-mutable original.
        """
        external: dict[str, SlotSpec] = {"k": SlotSpec(slot_type="str")}
        recipe = RecipeSpec(
            name="leak-probe",
            description="test fixture for external-mutation isolation",
            slots=external,
            build=lambda _slots: {},
        )
        external["leaked"] = SlotSpec(slot_type="str")
        assert "leaked" not in recipe.slots
        # The original key survives.
        assert "k" in recipe.slots

    def test_slots_remain_read_only_when_input_is_already_frozen(self) -> None:
        """Even when the input is already a ``MappingProxyType``, the
        resulting ``recipe.slots`` is itself a read-only mapping that
        rejects item assignment.

        ``deep_freeze`` always returns a *fresh* ``MappingProxyType`` for
        Mapping inputs (it detaches the proxy from any view onto a
        possibly-mutable backing dict), so we don't assert identity here
        — just that the read-only contract holds regardless of input
        shape.
        """
        from types import MappingProxyType

        already_frozen = MappingProxyType({"k": SlotSpec(slot_type="str")})
        recipe = RecipeSpec(
            name="frozen-input-probe",
            description="test fixture for already-frozen-input idempotency",
            slots=already_frozen,
            build=lambda _slots: {},
        )
        assert isinstance(recipe.slots, MappingProxyType)
        with pytest.raises(TypeError):
            recipe.slots["new"] = SlotSpec(slot_type="str")  # type: ignore[index]

    def test_slot_spec_is_itself_frozen(self) -> None:
        """``SlotSpec`` is ``frozen=True`` so attribute reassignment must raise.

        If a future refactor drops ``frozen=True`` on SlotSpec, the deep
        freeze on ``RecipeSpec.slots`` would still block dict-level
        mutation but slot fields could be reassigned in place — undermining
        the recipe-table integrity the freeze guard is meant to protect.
        """
        from dataclasses import FrozenInstanceError

        slot = SlotSpec(slot_type="str", required=True, description="probe")
        with pytest.raises(FrozenInstanceError):
            slot.required = False  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            slot.slot_type = "int"  # type: ignore[misc]


# --------------------------------------------------------------------------
# Regression: fixed CSV schema authored in the documented single-key
# ``{col: type}`` YAML form must NOT emit a false
# ``csv_fixed_schema_omits_observed_columns`` blocking diagnostic.
#
# Bug (now fixed in generation.py): ``compute_proof_diagnostics`` used to hand
# the RAW source-option field specs (e.g. ``[{"order_id": "str"}, ...]``)
# straight to ``_declared_field_name``, which does ``field.get("name")`` ->
# ``None`` for the ``{col: type}`` form. With every declared name reading as
# ``None``, ``derive_extra_column_risk`` saw the observed CSV headers as
# *undeclared* "extra" columns and — combined with
# ``on_validation_failure="discard"`` — emitted a FALSE blocking diagnostic
# claiming a fixed schema silently drops every row. The fix bridges the raw
# specs through ``get_raw_schema_config(...).to_dict()["fields"]`` (canonical
# ``{"name": ..., "type": ...}`` form) so ``_declared_field_name`` recovers
# the names and the declared columns are correctly recognised as matching the
# observed headers.
#
# This test pins the bug at its ACTUAL trigger point: it constructs a
# CompositionState whose CSV ``source.options.schema`` carries the raw
# ``{col: type}`` field form (the durable shape ``compute_proof_diagnostics``
# reads). Constructing the state directly — rather than routing through
# ``set_pipeline`` prevalidation — is load-bearing: prevalidation can
# normalise ``{col: type}`` into ``{name, type}``, which would let the OLD
# (buggy) ``.get("name")`` path *also* recover the names, so the false
# diagnostic would never fire and the test would pass on broken code. The raw
# form is exactly what must reach the production bridge to exercise the fix.
# --------------------------------------------------------------------------


class TestFixedSchemaSingleKeyFormNoFalseOmitDiagnostic:
    """A fixed CSV schema whose fields use the ``{col: type}`` form and match
    the bound blob's header row must produce NO false-omit diagnostic.
    """

    @pytest.fixture
    def _seeded_blob(self, tmp_path):
        from datetime import UTC, datetime

        from sqlalchemy.pool import StaticPool

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(engine)
        session_id = str(uuid4())
        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test",
                    created_at=now,
                    updated_at=now,
                )
            )

        blob_id = str(uuid4())
        storage_dir = tmp_path / "blobs" / session_id
        storage_dir.mkdir(parents=True)
        storage_path = storage_dir / f"{blob_id}_orders.csv"
        # Header row is exactly the columns the fixed schema declares below.
        body = b"order_id,customer,price\nO-1,Alice,49.95\nO-2,Bob,150.00\n"
        storage_path.write_bytes(body)
        with engine.begin() as conn:
            conn.execute(
                blobs_table.insert().values(
                    id=blob_id,
                    session_id=session_id,
                    filename="orders.csv",
                    mime_type="text/csv",
                    size_bytes=len(body),
                    content_hash=_content_hash(body),
                    storage_path=str(storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )
        return engine, session_id, blob_id

    def test_single_key_form_fixed_schema_matching_header_emits_no_false_omit(self, _seeded_blob) -> None:
        from elspeth.web.composer.state import (
            CompositionState,
            PipelineMetadata,
            SourceSpec,
        )
        from elspeth.web.composer.tools import compute_proof_diagnostics

        engine, session_id, blob_id = _seeded_blob

        # Fixed schema with fields in the documented single-key {col: type}
        # form — the exact authoring shape that triggered the false diagnostic.
        # The declared columns are exactly the blob's header row, and
        # on_validation_failure="discard" arms the omit-diagnostic emit site.
        source = SourceSpec(
            plugin="csv",
            on_success="classified",
            options={
                "blob_ref": blob_id,
                "schema": {
                    "mode": "fixed",
                    "fields": [
                        {"order_id": "str"},
                        {"customer": "str"},
                        {"price": "float"},
                    ],
                },
            },
            on_validation_failure="discard",
        )
        state = CompositionState(
            source=source,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        diagnostics = compute_proof_diagnostics(
            state,
            session_engine=engine,
            session_id=session_id,
        )

        codes = [d["code"] for d in diagnostics]
        # The load-bearing assertion: the false omit diagnostic must NOT fire.
        # On the reverted (buggy) code, the {col: type} declared names read as
        # None, the observed headers look undeclared, and this code is emitted.
        assert "csv_fixed_schema_omits_observed_columns" not in codes, diagnostics
        # The task also requires the sibling required-header mismatch code to be
        # absent (it does not fire here even on the buggy path, but assert it so
        # the test fully pins the "declared {col:type} fields match observed
        # headers" contract).
        assert "csv_source_blob_header_mismatch" not in codes, diagnostics


# --------------------------------------------------------------------------
# Recipe catalog content hash (tutorial cache input #5)
# --------------------------------------------------------------------------


def test_recipe_catalog_content_hash_covers_recipes_module() -> None:
    """The hash folds recipes.py byte content.

    recipes.py authors the deterministic pipeline including option-level
    content; it is the operator-controlled cache input that must be keyed.
    """
    recipes_path = Path(recipes_module.__file__)
    h = hashlib.sha256()
    h.update(recipes_path.read_bytes())
    assert recipe_catalog_content_hash() == h.hexdigest()


def test_recipe_catalog_content_hash_is_deterministic() -> None:
    assert recipe_catalog_content_hash() == recipe_catalog_content_hash()


class TestRecipeProviderGuard:
    """LLM recipes expose only opaque operator-approved profile aliases."""

    _CLASSIFY_SLOTS: ClassVar[dict] = {
        "classifier_template": "Classify {{ row.text }}.",
        "profile": "tutorial",
    }
    _WEB_SCRAPE_SLOTS: ClassVar[dict] = {
        "source_plugin": "json",
        "profile": "tutorial-default",
        "abuse_contact": "noreply@dta.gov.au",
        "scraping_reason": "DTA technical demonstration",
    }

    def test_classify_recipe_rejects_raw_provider_binding(self) -> None:
        with pytest.raises(RecipeValidationError, match=r"does not accept slot.*provider"):
            apply_recipe(
                "classify-rows-llm-jsonl",
                {"source_blob_id": str(uuid4()), **self._CLASSIFY_SLOTS, "provider": "azure"},
            )

    def test_classify_recipe_builds_with_opaque_profile(self) -> None:
        args = apply_recipe(
            "classify-rows-llm-jsonl",
            {"source_blob_id": str(uuid4()), **self._CLASSIFY_SLOTS},
        )
        llm_node = next(node for node in args["nodes"] if node["plugin"] == "llm")
        assert llm_node["options"]["profile"] == "tutorial"
        assert {"provider", "model", "api_key", "api_key_secret"}.isdisjoint(llm_node["options"])

    def test_web_scrape_recipe_rejects_raw_provider_binding(self) -> None:
        with pytest.raises(RecipeValidationError, match=r"does not accept slot.*provider"):
            apply_recipe(
                "web-scrape-llm-rate-jsonl",
                {"source_blob_id": str(uuid4()), **self._WEB_SCRAPE_SLOTS, "provider": "azure"},
            )

    def test_web_scrape_recipe_builds_with_opaque_profile(self) -> None:
        args = apply_recipe(
            "web-scrape-llm-rate-jsonl",
            {"source_blob_id": str(uuid4()), **self._WEB_SCRAPE_SLOTS},
        )
        llm_node = next(node for node in args["nodes"] if node["plugin"] == "llm")
        assert llm_node["options"]["profile"] == "tutorial-default"
        assert {"provider", "model", "api_key", "api_key_secret"}.isdisjoint(llm_node["options"])
