"""Separate the PromptShield P4 content-publication binding from other composer metadata.

Every composition-state insert binds a fresh ``content_publication`` entry into
``composer_meta`` (``sessions/content_publication.py``). Tests that pin the
*other* metadata a write preserves compare the remainder, and prove the binding
belongs to the exact persisted row instead of ignoring it.
"""

from __future__ import annotations

from typing import Any

from elspeth.contracts.composer_content import ContentPublication
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.sessions.content_publication import CONTENT_PUBLICATION_KEY, read_content_publication
from elspeth.web.sessions.protocol import CompositionStateRecord


def split_content_publication(record: CompositionStateRecord) -> tuple[ContentPublication, dict[str, Any]]:
    """Return the row-bound publication and the remaining composer metadata.

    ``read_content_publication`` raises unless the publication matches this
    record's session, state id, version and stored content hash.
    """
    publication = read_content_publication(record)
    assert publication is not None, "a persisted composition state must carry its content publication"
    remainder = dict(deep_thaw(record.composer_meta))
    del remainder[CONTENT_PUBLICATION_KEY]
    return publication, remainder
