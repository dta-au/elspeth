"""Session-local plugin schema disclosure state for Composer surfaces."""

from __future__ import annotations

from collections.abc import Callable

from elspeth.web.composer.session_cache import ComposerSessionCache, ComposerSessionCacheRegistry, SessionCacheKey


class SchemaDisclosureTracker:
    """Track delivered plugin schemas shared by planning and freeform turns.

    Bound to one operation's policy view. Saved sessions use the exact borrowed
    owner; uncached and local callers use a private bounded operation lifetime.
    """

    def __init__(self, cache: ComposerSessionCache | None = None, revision: Callable[[], str | None] = lambda: "local") -> None:
        self._cache = cache
        self._revision = revision
        self._operation_revision = revision()
        self._local = ComposerSessionCacheRegistry(enabled=False)
        self._pending: dict[str, tuple[str | None, str, str, str | None, int]] = {}
        self._pending_weight = 0

    def stage_plugin_schema(self, call_id: str, session_id: str | None, plugin_type: str, plugin_name: str) -> None:
        """Stage operation evidence; computation alone does not prove delivery."""
        revision = self._revision()
        weight = 128 + 4 * (len(call_id) + len(session_id or "") + len(plugin_type) + len(plugin_name) + len(revision or ""))
        previous = self._pending.pop(call_id, None)
        if previous is not None:
            self._pending_weight -= previous[4]
        # Omitted results and free logical repeats must not grow operation
        # bookkeeping without bound. Losing a convergence hint is conservative.
        if len(self._pending) >= 128 or self._pending_weight + weight > 64 * 1024:
            return
        self._pending[call_id] = (session_id, plugin_type, plugin_name, revision, weight)
        self._pending_weight += weight

    def confirm_delivery(self, delivered_tool_ids: frozenset[str]) -> None:
        """Credit only results included in a successful physical provider round."""
        for call_id in delivered_tool_ids:
            pending = self._pending.pop(call_id, None)
            if pending is not None:
                session_id, kind, name, revision, weight = pending
                self._pending_weight -= weight
                if revision == self._revision():
                    self.mark_plugin_schema_loaded(session_id, kind, name)

    def schemas_loaded_for_session(self, session_id: str | None) -> frozenset[tuple[str, str]]:
        """Return the immutable view of plugins whose schema has loaded.

        Returns an empty frozenset when ``session_id`` is None (the
        unsaved-session fast path) or when no ``get_plugin_schema`` call
        has yet been delivered for this session. The returned frozenset is a
        snapshot — subsequent ``mark_plugin_schema_loaded`` calls do not
        mutate it.
        """
        if session_id is None:
            return frozenset()
        revision = self._revision()
        if self._cache is not None and revision is not None:
            return self._cache.schemas_loaded(revision)
        with self._local.borrow(SessionCacheKey("operation", session_id)) as lease:
            return lease.cache.schemas_loaded(revision or "local") if lease.cache is not None else frozenset()

    def mark_plugin_schema_loaded(
        self,
        session_id: str | None,
        plugin_type: str,
        plugin_name: str,
    ) -> None:
        """Record confirmed provider delivery of a successful plugin schema.

        No-op when ``session_id`` is None (unsaved sessions have no
        persistent identity for the tracker; the next turn would not see
        the marking anyway).
        """
        if session_id is None:
            return
        revision = self._revision()
        # A producer may have started before a catalog/profile change. Keep
        # delivery conservative for the rest of that operation; the next fresh
        # view can establish guidance for the new projection.
        if revision != self._operation_revision:
            return
        if self._cache is not None and revision is not None:
            self._cache.mark_schema_loaded(revision, plugin_type, plugin_name)
            return
        with self._local.borrow(SessionCacheKey("operation", session_id)) as lease:
            if lease.cache is not None:
                lease.cache.mark_schema_loaded(revision or "local", plugin_type, plugin_name)
