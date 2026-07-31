# Sanitized fixtures

Onboarding step 3 ("implement mappings against locally supplied sanitized
fixtures") expects real request/response/error JSON bodies from your
agency's actual upstream API, with any secret or identifying content
scrubbed, dropped here as `.json` files. None are shipped with this
template — the gateway repository must never contain, or need, real
agency payloads to demonstrate its own onboarding path. Populate this
directory in your own copy of the scaffold, then write `tests/test_adapter.py`
assertions (after renaming from `test_adapter.py.template`) that load and
exercise them.
