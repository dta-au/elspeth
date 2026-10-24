"""Session-local plugin schema disclosure state for Composer surfaces."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from elspeth.contracts.errors import AuditIntegrityError


class SchemaDisclosureSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    session_id: str
    catalog_hash: str
    policy_hash: str
    schemas: tuple[tuple[str, str], ...]

    @model_validator(mode="after")
    def require_unique_schemas(self) -> SchemaDisclosureSnapshot:
        if len(set(self.schemas)) != len(self.schemas):
            raise ValueError("schema disclosure snapshot repeats a plugin")
        return self


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

    def snapshot_for_continuation(
        self,
        *,
        session_id: str,
        catalog_hash: str,
        policy_hash: str,
    ) -> SchemaDisclosureSnapshot:
        return SchemaDisclosureSnapshot(
            session_id=session_id,
            catalog_hash=catalog_hash,
            policy_hash=policy_hash,
            schemas=tuple(sorted(self.schemas_loaded_for_session(session_id))),
        )

    def restore_session_for_continuation(
        self,
        snapshot: SchemaDisclosureSnapshot,
        *,
        session_id: str,
        catalog_hash: str,
        policy_hash: str,
    ) -> None:
        if type(snapshot) is not SchemaDisclosureSnapshot:
            raise TypeError("schema disclosure requires its owned snapshot")
        if (snapshot.session_id, snapshot.catalog_hash, snapshot.policy_hash) != (
            session_id,
            catalog_hash,
            policy_hash,
        ):
            raise AuditIntegrityError("Schema disclosure identity changed; retain history and acquire current schemas before resuming.")
        self._schemas_loaded_by_session[session_id] = set(snapshot.schemas)
