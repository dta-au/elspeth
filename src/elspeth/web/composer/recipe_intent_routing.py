"""Compatibility projection for the registry-owned recipe intent matcher."""

from __future__ import annotations

from elspeth.web.composer.recipes import (
    InlineRecipeBlob as InlineRecipeBlob,
)
from elspeth.web.composer.recipes import (
    RecipeIntentContext,
    match_registered_recipe_intent,
)
from elspeth.web.composer.recipes import (
    RecipeIntentMatch as FreeformRecipeIntentMatch,
)


def match_freeform_recipe_intent(message: str) -> FreeformRecipeIntentMatch | None:
    """Match a freeform request through the surface-neutral recipe registry."""

    return match_registered_recipe_intent(RecipeIntentContext(user_text=message))
