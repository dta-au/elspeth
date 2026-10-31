"""Prepare content applicability inside the existing immutable-state transaction.

No provider, detector, service configuration or database access belongs here.
Every insert derives fresh version-bound metadata; historical rows stay intact.
"""

from dataclasses import replace
from uuid import UUID

from elspeth.contracts.composer_content import ContentPublication, content_storage_digest
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.sessions.protocol import CompositionStateData, CompositionStateRecord

CONTENT_PUBLICATION_KEY = "content_publication"


def storage_content_hash(state: CompositionStateData | CompositionStateRecord) -> str:
    """Bind stored authored columns without normalizing or rewriting old graphs."""
    if type(state) not in (CompositionStateData, CompositionStateRecord):
        raise AuditIntegrityError("Content publication requires an owned state snapshot")
    return content_storage_digest(
        {
            "legacy_source": deep_thaw(state.source) if type(state) is CompositionStateRecord else None,
            "sources": deep_thaw(state.sources),
            "nodes": deep_thaw(state.nodes),
            "edges": deep_thaw(state.edges),
            "outputs": deep_thaw(state.outputs),
            "metadata": deep_thaw(state.metadata_),
        }
    )


def prepare_content_publication(
    state: CompositionStateData,
    *,
    session_id: UUID,
    state_id: UUID,
    state_version: int,
) -> CompositionStateData:
    """Invalidate inherited applicability and bind the actual allocated version.

    A malformed present owned binding is corruption, not historical absence.
    A well-formed old binding may belong to a predecessor or fork parent and
    is deliberately replaced. Unrelated subsystem metadata is preserved.
    """
    if type(state) is not CompositionStateData:
        raise AuditIntegrityError("Content publication requires an owned state snapshot")
    metadata = dict(deep_thaw(state.composer_meta)) if state.composer_meta is not None else {}
    if CONTENT_PUBLICATION_KEY in metadata:
        ContentPublication.from_payload(metadata[CONTENT_PUBLICATION_KEY])
    publication = ContentPublication(session_id, state_id, state_version, storage_content_hash(state))
    metadata[CONTENT_PUBLICATION_KEY] = publication.to_payload()
    return replace(state, composer_meta=metadata)


def read_content_publication(state: CompositionStateRecord) -> ContentPublication | None:
    """Validate current applicability; absence remains uninspected legacy data."""
    if type(state) is not CompositionStateRecord:
        raise AuditIntegrityError("Content publication requires an owned persisted snapshot")
    metadata = state.composer_meta
    if metadata is None or CONTENT_PUBLICATION_KEY not in metadata:
        return None
    publication = ContentPublication.from_payload(metadata[CONTENT_PUBLICATION_KEY])
    expected = ContentPublication(state.session_id, state.id, state.version, storage_content_hash(state))
    if publication != expected:
        raise AuditIntegrityError("Content publication does not match its immutable graph version")
    return publication
