"""Test helpers for observing validation-only plugin lifecycle ownership."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from elspeth.contracts import SinkProtocol, SourceProtocol, TransformProtocol
    from elspeth.plugins.infrastructure.base import BaseSink, BaseSource, BaseTransform
    from elspeth.plugins.infrastructure.manager import PluginManager


@dataclass
class TrackedPlugin:
    """Close-count record for one plugin instance created during validation.

    A record, deliberately not a proxy. The instance handed back to the code
    under test IS the real plugin; only its ``close`` is rebound, per instance,
    to count the call and then run the class's own ``close``. A forwarding
    wrapper would have to impersonate whichever of ``BaseTransform`` /
    ``BaseSink`` / ``BaseSource`` it happened to hold — roughly seventy
    members between them — and any ``isinstance`` check in the validation path
    would see the wrapper rather than the plugin.
    """

    _delegate: BaseSink | BaseSource | BaseTransform
    close_count: int = 0


class _PluginManagerDelegate:
    """Explicit, exhaustive delegation of the ``PluginManager`` public surface.

    Every method a caller may reach is written out. There is no catch-all
    forwarding: a plugin-manager method that grows a new caller in the web
    layer fails here loudly, which is the point — a silent forward would let a
    double answer for a registry it was never checked against.
    """

    def __init__(self, delegate: PluginManager) -> None:
        self._delegate = delegate

    def register_builtin_plugins(self) -> None:
        self._delegate.register_builtin_plugins()

    def register(self, plugin: Any) -> None:
        self._delegate.register(plugin)

    def get_sources(self) -> list[type[SourceProtocol]]:
        return self._delegate.get_sources()

    def get_transforms(self) -> list[type[TransformProtocol]]:
        return self._delegate.get_transforms()

    def get_sinks(self) -> list[type[SinkProtocol]]:
        return self._delegate.get_sinks()

    def get_source_by_name(self, name: str) -> type[SourceProtocol]:
        return self._delegate.get_source_by_name(name)

    def get_transform_by_name(self, name: str) -> type[TransformProtocol]:
        return self._delegate.get_transform_by_name(name)

    def get_sink_by_name(self, name: str) -> type[SinkProtocol]:
        return self._delegate.get_sink_by_name(name)

    def create_source(self, source_type: str, config: dict[str, Any]) -> SourceProtocol:
        return self._delegate.create_source(source_type, config)

    def create_transform(self, transform_type: str, config: dict[str, Any]) -> TransformProtocol:
        return self._delegate.create_transform(transform_type, config)

    def create_sink(self, sink_type: str, config: dict[str, Any]) -> SinkProtocol:
        return self._delegate.create_sink(sink_type, config)


class TrackingPluginManager(_PluginManagerDelegate):
    """Delegate plugin-manager operations while tracking every created plugin.

    All three factories are tracked, not just ``create_transform``. Composer
    validation probes transforms, sinks (``_semantic_validator``'s output loop)
    and sources, and every probe instance is owned by the caller and must be
    closed. Tracking only one factory would leave a leaked sink or source probe
    invisible to ``instances`` — the exact defect the close-count assertions
    exist to catch.
    """

    def __init__(self, delegate: PluginManager) -> None:
        super().__init__(delegate)
        self.instances: list[TrackedPlugin] = []

    def _track(self, plugin: Any) -> Any:
        """Record ``plugin`` and count its closes, returning the plugin itself."""
        tracked = TrackedPlugin(plugin)
        self.instances.append(tracked)
        plugin_close = plugin.close

        def counting_close() -> None:
            tracked.close_count += 1
            plugin_close()

        plugin.close = counting_close
        return plugin

    def create_source(self, source_type: str, config: dict[str, Any]) -> SourceProtocol:
        return self._track(self._delegate.create_source(source_type, config))

    def create_transform(self, transform_type: str, config: dict[str, Any]) -> TransformProtocol:
        return self._track(self._delegate.create_transform(transform_type, config))

    def create_sink(self, sink_type: str, config: dict[str, Any]) -> SinkProtocol:
        return self._track(self._delegate.create_sink(sink_type, config))


@lru_cache(maxsize=1)
def real_plugin_manager() -> PluginManager:
    """A real, fully-registered PluginManager, built once per test session.

    Deliberately NOT ``get_shared_plugin_manager()``: the tests that use this
    have monkeypatched that function, and the point is to reach past the patch
    to a genuine registry.
    """
    from elspeth.plugins.infrastructure.manager import PluginManager

    manager = PluginManager()
    manager.register_builtin_plugins()
    return manager


class DelegatingPluginManagerDouble(_PluginManagerDelegate):
    """Plugin-manager double that breaks only what its subclass overrides.

    Composer validation reaches the shared manager for transforms, sinks and
    sources. A double that answers ``create_transform`` alone fails with an
    ``AttributeError`` naming a registry the test never intended to break,
    which reads as a defect in the code under test rather than a gap in the
    double. Everything not explicitly overridden delegates to a real registry,
    so each double breaks exactly the surface it names.
    """

    def __init__(self, delegate: PluginManager | None = None) -> None:
        super().__init__(real_plugin_manager() if delegate is None else delegate)
