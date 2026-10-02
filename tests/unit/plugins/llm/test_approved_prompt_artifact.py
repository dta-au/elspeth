"""Approval links identify effective prompts, independently of query audit hashes."""

import pytest
from pydantic import ValidationError

from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.schema import SchemaConfig
from elspeth.core.prompt_artifact import approved_prompt_artifact_hash
from elspeth.plugins.transforms.llm.base import LLMConfig


def test_effective_artifact_ignores_unused_fallback_but_binds_every_sent_template() -> None:
    queries = (("decorator", "Decorate {{ row.colour }}"), ("assistant", "Complement {{ row.colour }}"))
    original = approved_prompt_artifact_hash(prompt_template=None, system_prompt="Be precise", queries=queries)
    assert original == approved_prompt_artifact_hash(prompt_template="unused", system_prompt="Be precise", queries=queries)
    assert original != stable_hash("unused")
    assert original != stable_hash(queries[0][1])
    assert original == approved_prompt_artifact_hash(prompt_template=None, system_prompt="Be precise", queries=tuple(reversed(queries)))
    variants = (
        ("Different persona", queries),
        ("Be precise", (("renamed", queries[0][1]), queries[1])),
        ("Be precise", ((queries[0][0], "Changed template"), queries[1])),
    )
    for system, variant in variants:
        assert original != approved_prompt_artifact_hash(prompt_template=None, system_prompt=system, queries=variant)


def test_mixed_queries_bind_fallback() -> None:
    queries = (("override", "Own"), ("fallback", None))
    assert approved_prompt_artifact_hash(prompt_template="A", system_prompt=None, queries=queries) != approved_prompt_artifact_hash(
        prompt_template="B", system_prompt=None, queries=queries
    )


@pytest.mark.parametrize("queries", [None, {"fallback": {"input_fields": {"colour": "colour"}}}])
def test_missing_effective_prompt_is_attributed_to_the_required_fallback(queries: object) -> None:
    with pytest.raises(ValidationError) as exc:
        LLMConfig.model_validate(
            {
                "provider": "azure",
                "queries": queries,
                "schema_config": SchemaConfig.from_dict({"mode": "observed"}),
                "required_input_fields": [],
            }
        )
    assert [(error["loc"], error["type"]) for error in exc.value.errors()] == [(("prompt_template",), "missing")]


@pytest.mark.parametrize("provider", ["azure", "openrouter", "bedrock", "gateway"])
def test_config_artifact_validates_for_each_provider_and_rejects_system_drift(provider: str) -> None:
    artifact = approved_prompt_artifact_hash(prompt_template=None, system_prompt="Decorator", queries=(("answer", "Choose a colour"),))
    options = {
        "provider": provider,
        "system_prompt": "Decorator",
        "queries": {"answer": {"input_fields": {"colour": "colour"}, "template": "Choose a colour"}},
        "schema_config": SchemaConfig.from_dict({"mode": "observed"}),
        "required_input_fields": [],
        "approved_prompt_artifact_hash": artifact,
    }
    assert LLMConfig(**options).approved_prompt_artifact_hash == artifact
    with pytest.raises(ValidationError, match="effective prompt artifact"):
        LLMConfig(**{**options, "system_prompt": "Assistant"})


def test_runtime_yaml_path_rewrite_preserves_approved_artifact() -> None:
    import yaml

    from elspeth.web.execution.preflight import resolve_runtime_yaml_paths
    from elspeth.web.interpretation_state import approved_prompt_artifact_hash_from_options

    options = {
        "provider": "azure",
        "system_prompt": "Decorator",
        "queries": {
            "zebra": {"input_fields": {"colour": "colour"}, "template": "Choose {{ row.colour }}"},
            "antelope": {"input_fields": {"colour": "colour"}, "template": "Describe {{ row.colour }}"},
        },
        "schema": {"mode": "observed"},
        "required_input_fields": ["colour"],
    }
    artifact = approved_prompt_artifact_hash_from_options(options)
    options["approved_prompt_artifact_hash"] = artifact
    original = yaml.safe_dump({"transforms": [{"plugin": "llm", "options": options}]}, sort_keys=False)
    rewritten = yaml.safe_load(resolve_runtime_yaml_paths(original, "/tmp"))
    runtime_options = rewritten["transforms"][0]["options"]
    assert list(options["queries"]) == ["zebra", "antelope"]
    assert list(runtime_options["queries"]) == ["antelope", "zebra"]
    assert LLMConfig.from_dict(runtime_options).approved_prompt_artifact_hash == artifact
