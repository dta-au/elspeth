"""Process policy for LiteLLM's import-time pricing catalogue."""

import os


def configure_litellm_pricing() -> None:
    """Use bundled pricing unless the operator explicitly allows remote fetch."""
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
