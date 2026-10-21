# Composer content publication (0.8.10)

New immutable composition versions store a `content_publication` member in
`composer_meta` alongside their graph columns. The existing fenced state
transaction allocates the version, derives the binding and inserts both
together. A storage failure rolls back both.

The version-1 binding contains `session_id`, `state_id`, `state_version`,
`storage_content_hash` and `applicability`, plus `schema_version`. The storage
hash covers the stored authored graph columns, including historical nulls,
source declaration order, structural branch order and JSON numeric types.
Integer `1`, floating-point `1.0` and boolean `true` remain distinct, and
otherwise supported JSON integers retain their full value. It is separate from
the reconstructed graph's `composition_content_hash` and from diagnostic
complete-value references. It contains no graph text or detector verdict.

Applicability is always `requires_current_inspection`. Every edit, import,
interpretation resolution, fork and revert receives a new version binding.
Reverting to identical content can restore the storage hash; it does not
restore a previous version's applicability or inspection permission. Other
subsystems' metadata remains intact. A digest is an identity check within
the trusted store, not an authentication signature.

Historical absence remains supported and means there is no publication
binding. Malformed or mismatched present bindings are integrity failures.
Ordinary reads and writes do not silently replace corrupted present metadata.
The separate graph-only recovery path retains its existing preserve/refuse
rules.

Authenticated state operations remain available without a model or detector
call. The existing revert operation can select the current state itself to
repair absent legacy publication metadata as a new immutable version. Its
ownership, expected-head, operation-receipt and replay checks still apply;
the old row is unchanged. YAML replacement uses the existing import validation
and custody checks. Export does not require inspection clearance and keeps
its existing credential, path and blob-custody redaction.

For a historical row using the singular `source` column, an explicit revert
copies that source into the supported `sources` map in the newly appended
version. It preserves the source configuration and leaves the historical row
untouched; the new storage representation therefore has its own storage hash.

Stored publication metadata never authorizes provider dispatch. Final request
preparation, credential screening, Prompt Shield inspection and operation
admission still run for each actual provider use. Offline storage and route
tests do not establish detector accuracy or live provider acceptance.
