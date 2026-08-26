"""Shared field-name defaults for the blob-expand transform family.

``blob_fetch`` WRITES ``blob_ref`` and ``blob_content_type``; every expander
READS them. Those two spellings are one contract across four plugins, so they
live here rather than being retyped per plugin — a default that drifts on one
side of the pair silently breaks the chain the plugins exist to form, and the
failure surfaces as "blob not found" several nodes downstream rather than as a
mismatch anyone can see.

This module deliberately declares no class: the plugin discovery scan
(``plugins/infrastructure/discovery.py``) admits a file only when it holds a
``BaseTransform`` subclass carrying a ``name``, so a constants module is not
mistaken for a plugin — the same reason ``web_scrape_errors.py`` and
``web_scrape_fingerprint.py`` need no exclusion entry.
"""

from __future__ import annotations

from typing import Final

# Written by blob_fetch, read by every expander. Changing either spelling is a
# cross-plugin contract change, not a local default.
DEFAULT_BLOB_REF_FIELD: Final[str] = "blob_ref"
DEFAULT_BLOB_CONTENT_TYPE_FIELD: Final[str] = "blob_content_type"

# Descriptions are shared too: the composer surfaces them verbatim, and two
# plugins describing the same field differently reads as two different fields.
BLOB_REF_FIELD_DESCRIPTION: Final[str] = "Input field containing a payload-store content hash."
BLOB_CONTENT_TYPE_FIELD_DESCRIPTION: Final[str] = (
    "Input field containing the blob's normalized Content-Type, used to select the parse format."
)

# The inline arm: an expander may read its bytes from a ROW FIELD instead of the
# payload store. Both arms parse identically; only the source of the bytes
# differs. `source` is the discriminator every expander in the family exposes.
DEFAULT_TEXT_FIELD: Final[str] = "content"
TEXT_FIELD_DESCRIPTION: Final[str] = "Input row field holding the text to parse, when source is 'field'."
