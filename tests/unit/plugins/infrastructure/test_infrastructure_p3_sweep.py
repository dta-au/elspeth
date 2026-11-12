"""Regression tests for the 20261111-plugins-review P3 infrastructure findings.

Each test names the sweep finding it pins. Every test failed at an assertion
on the lane base and passes after that finding's fix commit.
"""

from __future__ import annotations

from elspeth.contracts.enums import Determinism
from elspeth.plugins.infrastructure.hookspecs import hookimpl
from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.plugins.infrastructure.validation import UnknownPluginTypeError
from elspeth.plugins.transforms.passthrough import PassThrough

# ---------------------------------------------------------------------------
# Finding #126: PluginManager.create_* must validate against its OWN registry
# ---------------------------------------------------------------------------


class _PrivatePassThrough(PassThrough):
    """A transform registered only on a private PluginManager, never on the shared one."""

    name = "lane12_private_passthrough"
    determinism = Determinism.DETERMINISTIC


class _PrivateTransformHooks:
    @hookimpl
    def elspeth_get_transforms(self) -> list[type[_PrivatePassThrough]]:
        return [_PrivatePassThrough]


def test_create_transform_on_private_manager_validates_against_its_own_registry() -> None:
    """Finding #126: a plugin registered only on this manager must be creatable.

    create_transform used to validate by resolving the class through the
    SHARED manager, so a plugin the instance itself resolves was refused as
    an unknown type before the instance's own lookup ever ran.
    """
    manager = PluginManager()
    manager.register(_PrivateTransformHooks())
    assert manager.get_transform_by_name("lane12_private_passthrough") is _PrivatePassThrough

    try:
        created = manager.create_transform("lane12_private_passthrough", {"schema": {"mode": "observed"}})
    except UnknownPluginTypeError as exc:
        raise AssertionError(f"create_transform consulted a registry other than its own: {exc}") from exc

    assert type(created) is _PrivatePassThrough


def test_create_transform_on_private_manager_still_refuses_invalid_config() -> None:
    """Finding #126: validation still runs (against the private class's model)."""
    manager = PluginManager()
    manager.register(_PrivateTransformHooks())

    try:
        manager.create_transform("lane12_private_passthrough", {"schema": {"mode": "observed"}, "not_a_knob": 1})
    except UnknownPluginTypeError as exc:
        raise AssertionError(f"create_transform consulted a registry other than its own: {exc}") from exc
    except ValueError as exc:
        assert "not_a_knob" in str(exc)
    else:
        raise AssertionError("an unknown config key must be refused")
