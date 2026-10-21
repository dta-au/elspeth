# Composer graph checksum

The Graph tab shows the saved composition's SHA-256 checksum. **Copy checksum**
copies the full value. **Match clipboard** compares it with clipboard text and
shows a check or cross with a text result. Clipboard access occurs only when a
button is pressed. If clipboard reading is unavailable or denied, **Paste
checksum** opens a manual comparison field. Empty or malformed text is reported
separately from a different valid checksum.

The server supplies `composition_content_hash` on composition state responses.
It uses the existing `composition_content_hash` producer in
`src/elspeth/web/composer/pipeline_proposal.py`, rather than a second browser
hash algorithm. The checksum covers authored sources, nodes, edges, outputs and
pipeline metadata, including names, descriptions and options. Ordinary object
key order is canonicalized. Source declaration order, node/edge/output sequence
order and mapping-form row-union/coalesce branch order remain significant.

Revision numbers, session/state identifiers, timestamps, validation results,
Composer operational metadata and local graph layout are outside this content
identity. The checksum is calculated before source storage-path redaction.
Blob references and stored source paths therefore count as content; rebinding
or uploading a file again can change the checksum even when the diagram looks
the same. Equivalent content means the same canonical authored representation,
not every graph that might produce equivalent runtime results.

A matching checksum establishes content equality under this contract. It does
not establish approval, successful validation, provider behavior or execution.
Comparison feedback clears when the displayed state or checksum changes, and
pending clipboard results cannot reinstate feedback for an older state.

New servers supply a lowercase 64-character hexadecimal checksum for states
with authored metadata. The existing read API also permits records with null
metadata; these remain readable and return a null checksum, displayed as
unavailable. Their conversion to runnable compositions still refuses null
metadata. The frontend also accepts an absent checksum from an older server
without computing a substitute. A present malformed value, including a null
checksum with populated metadata, is rejected at the frontend API boundary.
No stored graph format or database migration is introduced by this response
field.
