"""Owned capability declarations retain enums and an exact boolean only."""

import pytest

from elspeth.contracts.plugin_capabilities import CapabilityDeclaration, ControlRole, PluginCapability


@pytest.mark.parametrize(
    "capability,role,blocking",
    [
        (PluginCapability.LLM, None, False),
        (PluginCapability.PROMPT_SHIELD, ControlRole.INPUT, False),
        (PluginCapability.PROMPT_SHIELD, ControlRole.INPUT, True),
        (PluginCapability.CONTENT_SAFETY, ControlRole.OUTPUT, False),
        (PluginCapability.CONTENT_SAFETY, ControlRole.OUTPUT, True),
    ],
)
def test_current_capability_declarations_keep_their_exact_owned_values(capability, role, blocking):
    value = CapabilityDeclaration(capability, role, blocking)
    assert value.capability is capability
    assert value.control_role is role
    assert value.blocks_positive_detection is blocking


@pytest.mark.parametrize("blocking", [False, True])
def test_string_llm_cannot_bypass_the_nominal_capability_and_blocking_rules(blocking):
    assert CapabilityDeclaration(PluginCapability.LLM).capability is PluginCapability.LLM
    with pytest.raises(TypeError, match="requires a PluginCapability"):
        CapabilityDeclaration("llm", None, blocking)
    with pytest.raises(ValueError, match="LLM capability cannot claim control blocking"):
        CapabilityDeclaration(PluginCapability.LLM, None, True)


@pytest.mark.parametrize("blocking", [0, 1, "false", PluginCapability.LLM])
def test_blocking_flag_cannot_retain_an_integer_string_or_enum(blocking):
    assert CapabilityDeclaration(PluginCapability.PROMPT_SHIELD, ControlRole.INPUT, True).blocks_positive_detection is True
    with pytest.raises(TypeError, match="blocking flag must be a bool"):
        CapabilityDeclaration(PluginCapability.PROMPT_SHIELD, ControlRole.INPUT, blocking)


def test_foreign_capability_is_refused_before_its_hash_or_equality_runs():
    assert CapabilityDeclaration(PluginCapability.LLM).blocks_positive_detection is False
    effects: list[str] = []

    class ForeignCapability:
        def __hash__(self):
            effects.append("hash")
            return hash(PluginCapability.LLM)

        def __eq__(self, other):
            effects.append("equality")
            return other is PluginCapability.LLM

    with pytest.raises(TypeError, match="requires a PluginCapability"):
        CapabilityDeclaration(ForeignCapability())
    assert effects == []


def test_foreign_blocking_object_is_refused_without_truthiness_projection():
    assert CapabilityDeclaration(PluginCapability.CONTENT_SAFETY, ControlRole.OUTPUT, False).blocks_positive_detection is False
    effects: list[str] = []

    class ForeignBlocking:
        def __bool__(self):
            effects.append("truthiness")
            return False

    with pytest.raises(TypeError, match="blocking flag must be a bool"):
        CapabilityDeclaration(PluginCapability.CONTENT_SAFETY, ControlRole.OUTPUT, ForeignBlocking())
    assert effects == []


def test_existing_role_coherence_refusal_is_preserved():
    assert CapabilityDeclaration(PluginCapability.PROMPT_SHIELD, ControlRole.INPUT).control_role is ControlRole.INPUT
    with pytest.raises(ValueError, match="invalid control role"):
        CapabilityDeclaration(PluginCapability.PROMPT_SHIELD, ControlRole.OUTPUT)
