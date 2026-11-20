"""Write-time size limit for new inline LLM prompt text."""

from __future__ import annotations

import re
from collections.abc import Mapping

from elspeth.web.composer.state import CompositionState, NodeSpec
from elspeth.web.execution._validation_materialization import llm_prompt_file_scalar_field

INLINE_PROMPT_MAX_WORDS = 100
INLINE_PROMPT_MAX_CHARACTERS = 1_000

_JINJA_TAG = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.DOTALL)
_AUTHORED_TOKEN = re.compile(r"[^\W_]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def inline_prompt_counts(value: str) -> tuple[int, int]:
    """Count prose after removing Jinja tags and collapsing whitespace."""
    prose = _WHITESPACE.sub(" ", _JINJA_TAG.sub("", value)).strip()
    return sum(bool(_AUTHORED_TOKEN.search(token)) for token in prose.split()), len(prose)


def _prompt_values(node: NodeSpec) -> dict[str, str]:
    if node.plugin != "llm":
        return {}
    options = node.options
    values: dict[str, str] = {}
    for name in ("prompt_template", "system_prompt"):
        value = options.get(name)
        if type(value) is str:
            values[name] = value
    queries = options.get("queries")
    if isinstance(queries, (list, tuple)):
        for index, query in enumerate(queries):
            if isinstance(query, Mapping):
                value = query.get("template")
                if type(value) is str:
                    key = query.get("name")
                    field = f"queries.{key if type(key) is str and key else index}.template"
                    if llm_prompt_file_scalar_field(f"node:{node.id}.options.{field}") is not None:
                        values[field] = value
    elif isinstance(queries, Mapping):
        for key, query in queries.items():
            if isinstance(query, Mapping):
                value = query.get("template")
                if type(value) is str:
                    field = f"queries.{key}.template"
                    if llm_prompt_file_scalar_field(f"node:{node.id}.options.{field}") is not None:
                        values[field] = value
    return values


def new_long_inline_prompt(prior: CompositionState, proposed: CompositionState) -> tuple[str, str, int, int] | None:
    """Find the first changed covered prompt whose compiled value exceeds either cap."""
    previous = {node.id: node for node in prior.nodes}
    for node in proposed.nodes:
        old_node = previous.get(node.id)
        old_values = _prompt_values(old_node) if old_node is not None else {}
        for field, value in _prompt_values(node).items():
            # Flattening a structured prompt makes the compiled value new inline text.
            unchanged = old_values.get(field) == value
            if field == "prompt_template" and old_node is not None:
                unchanged = unchanged and (old_node.options.get("prompt_template_parts") is None) == (
                    node.options.get("prompt_template_parts") is None
                )
            if unchanged:
                continue
            words, characters = inline_prompt_counts(value)
            if words > INLINE_PROMPT_MAX_WORDS or characters > INLINE_PROMPT_MAX_CHARACTERS:
                return node.id, field, words, characters
    return None


def inline_prompt_refusal_message(field: str, words: int, characters: int) -> str:
    return (
        f"{field} has {words} words and {characters} prose characters; inline prompts allow at most "
        f"{INLINE_PROMPT_MAX_WORDS} words and {INLINE_PROMPT_MAX_CHARACTERS} characters. "
        "Put the complete text in a UTF-8 text file and bind the file to this prompt field."
    )
