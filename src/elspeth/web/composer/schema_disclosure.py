"""Session-local plugin schema disclosure state for Composer surfaces."""

from __future__ import annotations


class SchemaDisclosureTracker:
    """Track successful plugin-schema reads shared by planning and freeform turns.

    This is in-memory convergence guidance, scoped to one Composer service
    instance. Session operations serialize turns for a given session.
    """

    def __init__(self) -> None:
        self._schemas_loaded_by_session: dict[str, set[tuple[str, str]]] = {}

    def schemas_loaded_for_session(self, session_id: str | None) -> frozenset[tuple[str, str]]:
        """Return the immutable view of plugins whose schema has loaded.

        Returns an empty frozenset when ``session_id`` is None (the
        unsaved-session fast path) or when no ``get_plugin_schema`` call
        has yet succeeded for this session. The returned frozenset is a
        snapshot — subsequent ``mark_plugin_schema_loaded`` calls do not
        mutate it.
        """
        if session_id is None:
            return frozenset()
        if session_id not in self._schemas_loaded_by_session:
            return frozenset()
        return frozenset(self._schemas_loaded_by_session[session_id])

    def mark_plugin_schema_loaded(
        self,
        session_id: str | None,
        plugin_type: str,
        plugin_name: str,
    ) -> None:
        """Record that ``get_plugin_schema`` returned successfully for this plugin.

        No-op when ``session_id`` is None (unsaved sessions have no
        persistent identity for the tracker; the next turn would not see
        the marking anyway).
        """
        if session_id is None:
            return
        if session_id not in self._schemas_loaded_by_session:
            self._schemas_loaded_by_session[session_id] = set()
        self._schemas_loaded_by_session[session_id].add((plugin_type, plugin_name))
