"""Test helpers for observing validation-only plugin lifecycle ownership."""

from __future__ import annotations

from functools import lru_cache
from typing import Any


class TrackedPlugin:
    """Transparent plugin proxy that records every close call."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self.close_count = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def close(self) -> None:
        self.close_count += 1
        self._delegate.close()


class TrackingPluginManager:
    """Delegate plugin-manager operations while wrapping every created plugin.

    All three factories are wrapped, not just ``create_transform``. Composer
    validation probes transforms, sinks (``_semantic_validator``'s output loop)
    and sources, and every probe instance is owned by the caller and must be
    closed. Tracking only one factory would leave a leaked sink or source probe
    invisible to ``instances`` — the exact defect the close-count assertions
    exist to catch.
    """

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self.instances: list[TrackedPlugin] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def _track(self, plugin: Any) -> TrackedPlugin:
        tracked = TrackedPlugin(plugin)
        self.instances.append(tracked)
        return tracked

    def create_transform(self, plugin_name: str, options: dict[str, Any]) -> TrackedPlugin:
        return self._track(self._delegate.create_transform(plugin_name, options))

    def create_sink(self, plugin_name: str, options: dict[str, Any]) -> TrackedPlugin:
        return self._track(self._delegate.create_sink(plugin_name, options))

    def create_source(self, plugin_name: str, options: dict[str, Any]) -> TrackedPlugin:
        return self._track(self._delegate.create_source(plugin_name, options))


@lru_cache(maxsize=1)
def real_plugin_manager() -> Any:
    """A real, fully-registered PluginManager, built once per test session.

    Deliberately NOT ``get_shared_plugin_manager()``: the tests that use this
    have monkeypatched that function, and the point is to reach past the patch
    to a genuine registry.
    """
    from elspeth.plugins.infrastructure.manager import PluginManager

    manager = PluginManager()
    manager.register_builtin_plugins()
    return manager


class DelegatingPluginManagerDouble:
    """Plugin-manager double that breaks only what its subclass overrides.

    Composer validation reaches the shared manager for transforms, sinks and
    sources. A double that answers ``create_transform`` alone fails with an
    ``AttributeError`` naming a registry the test never intended to break,
    which reads as a defect in the code under test rather than a gap in the
    double. Everything not explicitly overridden delegates to a real registry,
    so each double breaks exactly the surface it names.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(real_plugin_manager(), name)
