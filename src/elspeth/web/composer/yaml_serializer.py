"""Stateless Composer YAML serialization dependency for runtime consumers."""

from __future__ import annotations

from elspeth.web.composer.state import CompositionState


class ComposerYamlSerializer:
    """Expose canonical serialization without retaining a module or callback."""

    __slots__ = ()

    def generate_yaml(self, state: CompositionState) -> str:
        # Resolve the canonical function at the call, as the existing module
        # dependency did. The adapter stores neither it nor the module.
        from elspeth.web.composer.yaml_generator import generate_yaml

        return generate_yaml(state)
