"""Local-only compiler for universal web plugin authorization."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Literal, NoReturn, Protocol

from elspeth.contracts.plugin_capabilities import CapabilityDeclaration, ControlMode, PluginCapability
from elspeth.plugins.infrastructure.base import BaseSink, BaseSource, BaseTransform
from elspeth.web.plugin_policy.models import PluginId, WebPluginPolicy
from elspeth.web.plugin_policy.profiles import RuntimeWebPluginConfig

_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z")
_SOURCE_HASH = re.compile(r"sha256:[0-9a-f]{16}\Z")

REQUIRED_WEB_PLUGIN_IDS = frozenset(
    {
        PluginId("source", "csv"),
        PluginId("source", "json"),
        PluginId("source", "llm"),
        PluginId("source", "text"),
        PluginId("sink", "csv"),
        # Paired with sink:text. text refuses any value carrying CR or LF, so
        # document is the only sink that can publish generated multiline text;
        # authorizing text alone leaves that request with no correct sink and
        # is what produced elspeth-afdf55a17c.
        PluginId("sink", "document"),
        PluginId("sink", "json"),
        PluginId("sink", "text"),
        PluginId("transform", "field_mapper"),
        # The remedies text and document name in their own guidance. A remedy
        # the surface does not authorize is one the Composer cannot take.
        PluginId("transform", "line_explode"),
        PluginId("transform", "llm"),
        PluginId("transform", "report_assemble"),
        PluginId("transform", "web_scrape"),
    }
)


type _PluginClass = BaseSource | BaseTransform | BaseSink


class PluginRegistry(Protocol):
    def get_sources(self) -> Sequence[type]: ...
    def get_transforms(self) -> Sequence[type]: ...
    def get_sinks(self) -> Sequence[type]: ...


def _fail(reason: str) -> NoReturn:
    raise ValueError(f"web plugin policy invalid: {reason}")


def _admit_registry_category(
    classes: Sequence[type],
    expected_base: type[_PluginClass],
    category: Literal["source", "transform", "sink"],
) -> dict[PluginId, type[_PluginClass]]:
    admitted: dict[PluginId, type[_PluginClass]] = {}
    for plugin_cls in classes:
        if not issubclass(plugin_cls, expected_base):
            _fail("plugin_category_mismatch")
        admitted[PluginId(category, plugin_cls.name)] = plugin_cls
    return admitted


def _registry_map(registry: PluginRegistry) -> dict[PluginId, type[_PluginClass]]:
    return {
        **_admit_registry_category(registry.get_sources(), BaseSource, "source"),
        **_admit_registry_category(registry.get_transforms(), BaseTransform, "transform"),
        **_admit_registry_category(registry.get_sinks(), BaseSink, "sink"),
    }


def _parse_unique(raw_values: Iterable[str]) -> tuple[PluginId, ...]:
    parsed: list[PluginId] = []
    seen: set[PluginId] = set()
    for raw in raw_values:
        try:
            plugin_id = PluginId.parse(raw)
        except ValueError:
            _fail("invalid_plugin_id")
        if plugin_id in seen:
            _fail("duplicate_plugin_id")
        seen.add(plugin_id)
        parsed.append(plugin_id)
    return tuple(parsed)


def _validate_identity(plugin_cls: type[_PluginClass]) -> tuple[str, str]:
    version = plugin_cls.plugin_version
    source_hash = plugin_cls.source_file_hash
    if not isinstance(version, str) or version == "0.0.0" or _VERSION.fullmatch(version) is None:
        _fail("invalid_plugin_version")
    if not isinstance(source_hash, str) or _SOURCE_HASH.fullmatch(source_hash) is None:
        _fail("invalid_plugin_source_hash")
    return version, source_hash


def compile_web_plugin_policy(*, registry: PluginRegistry, settings: RuntimeWebPluginConfig) -> WebPluginPolicy:
    """Compile settings against the complete installed registry without I/O."""
    installed = _registry_map(registry)
    allowlist = _parse_unique(settings.plugin_allowlist)
    optional = frozenset(allowlist)
    authorized = REQUIRED_WEB_PLUGIN_IDS | optional
    if not authorized <= installed.keys():
        _fail("plugin_not_installed")

    for profile in settings.operator_profiles:
        plugin_id = PluginId("transform", profile.plugin)
        if plugin_id in authorized and not profile.check_local_requirements().available:
            _fail("plugin_unavailable")

    identities = tuple((plugin_id, *_validate_identity(installed[plugin_id])) for plugin_id in sorted(authorized))
    implementations: dict[PluginCapability, set[PluginId]] = {capability: set() for capability in PluginCapability}
    for plugin_id in sorted(authorized):
        plugin_cls = installed[plugin_id]
        if not plugin_cls.check_web_local_requirements():
            _fail("plugin_unavailable")
        declarations = plugin_cls.policy_capabilities
        if not isinstance(declarations, frozenset) or any(not isinstance(item, CapabilityDeclaration) for item in declarations):
            _fail("invalid_capability_declaration")
        for declaration in declarations:
            implementations[declaration.capability].add(plugin_id)

    preferences: list[tuple[PluginCapability, tuple[PluginId, ...]]] = []
    for capability, raw_order in settings.plugin_preferences:
        ordered = _parse_unique(raw_order)
        if any(plugin_id not in authorized for plugin_id in ordered):
            _fail("preference_not_authorized")
        if any(plugin_id not in implementations[capability] for plugin_id in ordered):
            _fail("capability_mismatch")
        if set(ordered) != implementations[capability]:
            _fail("incomplete_preference_order")
        preferences.append((capability, ordered))

    modes = settings.plugin_control_modes
    preference_caps = {capability for capability, _ in preferences}
    for capability, mode in modes:
        if mode is ControlMode.REQUIRED and not implementations[capability]:
            _fail("required_control_unconfigured")
        if len(implementations[capability]) > 1 and capability not in preference_caps:
            _fail("incomplete_preference_order")

    return WebPluginPolicy.create(
        required=REQUIRED_WEB_PLUGIN_IDS,
        configured_optional=optional,
        preferences=tuple(preferences),
        control_modes=modes,
        plugin_code_identities=identities,
    )
