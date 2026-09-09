from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from elspeth.contracts.plugin_capabilities import ControlMode, PluginCapability
from elspeth.web.plugin_policy.models import PluginId, WebPluginPolicy


def test_plugin_id_is_kind_qualified_and_strict() -> None:
    assert PluginId.parse("transform:llm") == PluginId("transform", "llm")
    assert str(PluginId("sink", "json")) == "sink:json"

    for raw in ("llm", "Transform:llm", "transform:LLM", "gate:llm", "transform:llm-v2"):
        with pytest.raises(ValueError, match="invalid kind-qualified plugin id"):
            PluginId.parse(raw)


def test_plugin_id_for_name_returns_none_instead_of_raising_on_untrusted_names() -> None:
    """``for_name`` is the non-raising parse the web policy layer authors against."""
    assert PluginId.for_name("transform", "llm") == PluginId("transform", "llm")

    # Malformed authored names, and names that are not strings at all, are an
    # explicit absence rather than a ValueError driven as control flow.
    for name in ("LLM", "llm-v2", "", "1llm", "transform:llm"):
        assert PluginId.for_name("transform", name) is None
    for value in (None, 7, b"llm", ["llm"], {"name": "llm"}):
        assert PluginId.for_name("source", value) is None

    # for_name must reject EXACTLY what the constructor rejects. It re-states the
    # grammar rather than driving __post_init__, so this pairing is what stops the
    # two from drifting into a ValueError escaping an interface typed `| None`.
    for name in ("llm", "aws_s3", "field_mapper", "LLM", "llm-v2", "", "1llm", "transform:llm", "llm "):
        try:
            constructed: PluginId | None = PluginId("transform", name)
        except ValueError:
            constructed = None
        assert PluginId.for_name("transform", name) == constructed


def test_policy_is_deeply_immutable() -> None:
    policy = WebPluginPolicy.create(
        required=frozenset({PluginId("transform", "llm")}),
        configured_optional=frozenset(),
        preferences=((PluginCapability.PROMPT_SHIELD, ()),),
        control_modes=((PluginCapability.PROMPT_SHIELD, ControlMode.RECOMMEND),),
        plugin_code_identities=((PluginId("transform", "llm"), "1.0.0", "sha256:0123456789abcdef"),),
    )

    with pytest.raises(FrozenInstanceError):
        policy.policy_hash = "changed"  # type: ignore[misc]


def test_policy_hash_sorts_sets_but_preserves_preference_order() -> None:
    first = WebPluginPolicy.create(
        required=frozenset({PluginId("transform", "llm")}),
        configured_optional=frozenset({PluginId("sink", "database"), PluginId("transform", "azure_prompt_shield")}),
        preferences=(
            (
                PluginCapability.PROMPT_SHIELD,
                (PluginId("transform", "azure_prompt_shield"), PluginId("transform", "future_prompt_shield")),
            ),
        ),
        control_modes=((PluginCapability.PROMPT_SHIELD, ControlMode.RECOMMEND),),
        plugin_code_identities=((PluginId("transform", "llm"), "1.0.0", "sha256:0123456789abcdef"),),
    )
    reordered_set = WebPluginPolicy.create(
        required=first.required,
        configured_optional=frozenset(reversed(tuple(first.configured_optional))),
        preferences=first.preferences,
        control_modes=first.control_modes,
        plugin_code_identities=first.plugin_code_identities,
    )
    reversed_preference = WebPluginPolicy.create(
        required=first.required,
        configured_optional=first.configured_optional,
        preferences=((PluginCapability.PROMPT_SHIELD, tuple(reversed(first.preferences[0][1]))),),
        control_modes=first.control_modes,
        plugin_code_identities=first.plugin_code_identities,
    )

    assert first.policy_hash == reordered_set.policy_hash
    assert first.policy_hash != reversed_preference.policy_hash
