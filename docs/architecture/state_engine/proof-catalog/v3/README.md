# State-engine proof catalog v3

Catalog v3 uses catalog schema 2 and assessment schema 3. It replaces the v2
applicability profiles with an explicit policy for every
`(leg, case, profile, dimension)` cell. A required cell has a null reason; a
reviewed `not_applicable` cell has a non-empty catalog reason.

The catalog and assessment schemas in this directory are normative. Catalog
v3 preserves every v2 semantic contract outside PB-09. PB-09 cases additionally
carry a live plugin key and a closed provider/authentication variant identity.

This directory is a Task 12 input, not the maintained-current pointer. The
repository continues to identify v2 as current until the first full v3
assessment and its documentation pointers are published atomically.
