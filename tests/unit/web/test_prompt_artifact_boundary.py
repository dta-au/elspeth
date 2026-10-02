"""Untrusted prompt option shapes cannot acquire an artifact identity."""

from typing import Any

import pytest

from elspeth.core.prompt_artifact import approved_prompt_artifact_hash
from elspeth.web.interpretation_state import approved_prompt_artifact_hash_from_options


@pytest.mark.parametrize(
    "options",
    [
        {"prompt_template": 7},
        {"prompt_template": "Prompt", "system_prompt": ["persona"]},
        {"queries": {"query": {"template": 7}}},
        {"queries": {"query": {"template": "Prompt"}}, "system_prompt": ["persona"]},
    ],
)
def test_prompt_artifact_rejects_malformed_options(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        approved_prompt_artifact_hash_from_options(options)


def test_prompt_artifact_accepts_text_options() -> None:
    assert approved_prompt_artifact_hash_from_options({"prompt_template": "Prompt", "system_prompt": "Persona"}) == (
        approved_prompt_artifact_hash(prompt_template="Prompt", system_prompt="Persona")
    )
