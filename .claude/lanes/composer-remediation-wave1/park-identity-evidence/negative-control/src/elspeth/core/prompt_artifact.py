"""Content identity of the effective prompt artifact approved for an LLM node."""

from elspeth.contracts.hashing import stable_hash


def approved_prompt_artifact_hash(
    *,
    prompt_template: str | None,
    system_prompt: str | None,
    queries: tuple[tuple[str, str | None], ...] | None = None,
) -> str:
    """Bind approval provenance to system text and named effective queries.

    This node artifact is distinct from each rendered query's template hash.
    A fallback that no query uses contributes nothing to the artifact.
    Query names are canonicalized because YAML and execution-envelope writers
    may reorder mapping keys. Call ordering belongs to execution provenance,
    not the identity of this approved collection of named prompt templates.
    """
    effective_queries: list[tuple[str | None, str]] = []
    entries = tuple(sorted(queries, key=lambda query: query[0])) if queries is not None else ((None, prompt_template),)
    for name, override in entries:
        template = override if override is not None else prompt_template
        if template is None:
            raise ValueError(f"Query {name!r} requires prompt_template or its own template override")
        effective_queries.append((name, template))
    return stable_hash(
        {
            "domain": "elspeth.approved-prompt-artifact.v1",
            "system_prompt": system_prompt,
            "queries": effective_queries,
        }
    )
