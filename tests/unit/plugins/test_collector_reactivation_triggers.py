"""Tripwires for two collector findings that are UNREACHABLE / REFUTED today
only because of facts about the shipping plugin set.

Each finding was deliberately NOT dismissed on 2026-08-26: its containment is
incidental (which plugins happen to override a method, which config models
happen to carry no credential field), so the record needs a REACTIVATION
TRIGGER rather than a closure. A trigger written as prose plus a "cheap
standing check" nobody runs is itself the fail-open shape this epic exists
to burn down. These tests ARE the standing checks: when one goes red, the
finding it names is live again and the docstring says exactly why.

Do not "fix" a red test here by relaxing the assertion. Re-open the named
ticket and build the arm the finding describes.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from elspeth.contracts.enums import Determinism
from elspeth.core.secrets import SECRET_FIELD_NAMES, SECRET_FIELD_SUFFIXES
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.manager import PluginManager


def _builtin_transforms() -> tuple[type[BaseTransform], ...]:
    manager = PluginManager()
    manager.register_builtin_plugins()
    registered = tuple(manager.get_transforms())
    # Fail loudly rather than skip: a registered transform outside the
    # BaseTransform hierarchy would be invisible to both tripwires. mypy
    # treats TransformProtocol and BaseTransform as disjoint (structural vs
    # nominal), so the narrowing is a runtime MRO check plus one explicit
    # cast at this boundary rather than an issubclass() it would call
    # unreachable.
    foreign = [transform for transform in registered if BaseTransform not in transform.__mro__]
    assert foreign == [], f"registered transforms outside BaseTransform: {foreign!r}"
    return cast(tuple[type[BaseTransform], ...], registered)


def input_semantic_requirement_overriders(transforms: Iterable[type[BaseTransform]]) -> tuple[type[BaseTransform], ...]:
    """Transform classes that override ``input_semantic_requirements()``."""

    return tuple(
        transform for transform in transforms if transform.input_semantic_requirements is not BaseTransform.input_semantic_requirements
    )


def credential_shaped_fields(model: type) -> tuple[str, ...]:
    """Config-model field names the secret redactor would treat as credentials
    — the SAME vocabulary ``core/secrets.py`` redacts by, not a restatement."""

    names = tuple(getattr(model, "model_fields", {}))
    return tuple(name for name in names if name in SECRET_FIELD_NAMES or name.endswith(SECRET_FIELD_SUFFIXES))


# ── elspeth-52fd5cb874 ────────────────────────────────────────────────────


def test_no_batch_aware_transform_overrides_input_semantic_requirements() -> None:
    """elspeth-52fd5cb874 — reactivation trigger 1.

    ``web/composer/_semantic_validator.py`` walks consumer semantic
    requirements only for ``node_type == "transform"`` nodes, so a collector
    (and every other node kind) is skipped. That omission cannot fire today
    ONLY because the plugins that override ``input_semantic_requirements()``
    are all ``is_batch_aware=False``, and a collector cannot host a
    non-batch-aware plugin (``collector_plugin_not_batch_aware``). The moment
    a batch-aware plugin overrides the method, a collector can carry semantic
    requirements the validator never checks — a silent fail-open. This test
    goes red at that moment; nothing else in the tree would.
    """

    overriders = input_semantic_requirement_overriders(_builtin_transforms())
    assert overriders, "tripwire is vacuous: no builtin transform overrides input_semantic_requirements()"
    batch_aware_overriders = sorted(transform.name for transform in overriders if transform.is_batch_aware)
    assert batch_aware_overriders == [], (
        f"{batch_aware_overriders} are batch-aware AND override input_semantic_requirements(): "
        "a collector can now host semantic requirements that _semantic_validator.py skips. "
        "Re-open elspeth-52fd5cb874 and add the collector arm; do not relax this test."
    )


def test_the_override_detector_sees_an_override() -> None:
    """The tripwire above is only as good as its detector; prove the detector
    fires on a class that overrides the hook and stays quiet on one that
    inherits it."""

    class _Inherits(BaseTransform):
        name = "inherits"
        determinism = Determinism.DETERMINISTIC

    class _Overrides(BaseTransform):
        name = "overrides"
        determinism = Determinism.DETERMINISTIC

        def input_semantic_requirements(self):
            return super().input_semantic_requirements()

    assert input_semantic_requirement_overriders((_Inherits, _Overrides)) == (_Overrides,)


# ── elspeth-7f82775e9c ────────────────────────────────────────────────────


def test_no_batch_aware_config_model_carries_a_credential_shaped_field() -> None:
    """elspeth-7f82775e9c — reactivation triggers 1 and 2.

    ``tools/secrets.py`` ``wire_secret_ref`` refuses collector targets. The
    original leak argument (a collector must therefore inline a literal
    credential, which the audit DB persisted unredacted) was REFUTED — but
    the refusal is VACUOUS only while no batch-aware plugin can accept a
    credential at all: every ``is_batch_aware`` config model is
    ``extra="forbid"`` and none declares a field the redactor's own
    vocabulary (``SECRET_FIELD_NAMES`` / ``SECRET_FIELD_SUFFIXES``) treats as
    a secret. The moment either changes, a collector has a credential to
    carry and the ``wire_secret_ref`` refusal stops being a parity nit. This
    test goes red at that moment.
    """

    batch_aware = tuple(transform for transform in _builtin_transforms() if transform.is_batch_aware)
    assert batch_aware, "tripwire is vacuous: no builtin transform is batch-aware"
    offenders: dict[str, object] = {}
    for transform in batch_aware:
        model = transform.get_config_model()
        assert model is not None, f"{transform.name}: batch-aware transform without a config model"
        if model.model_config.get("extra") != "forbid":
            offenders[transform.name] = f"extra={model.model_config.get('extra')!r} (an unmodelled credential key can pass)"
        fields = credential_shaped_fields(model)
        if fields:
            offenders[transform.name] = f"credential-shaped field(s) {fields}"
    assert offenders == {}, (
        f"{offenders}: a collector can now carry a credential that wire_secret_ref refuses to wire. "
        "Re-open elspeth-7f82775e9c and land the collector target with a case that exercises it; do not relax this test."
    )


def test_the_credential_field_detector_uses_the_redactors_vocabulary() -> None:
    """Prove the detector fires on both the exact-name and the suffix forms
    the redactor recognises, and not on an ordinary field."""

    from pydantic import BaseModel

    class _Probe(BaseModel):
        text_field: str
        api_key: str
        openai_token: str

    assert credential_shaped_fields(_Probe) == ("api_key", "openai_token")
