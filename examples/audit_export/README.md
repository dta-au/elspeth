# Audit Export Example

This demonstrates ELSPETH's audit trail export feature for compliance and legal inquiry. After a run completes, the full audit trail is exported to a JSON sink for review.

## Running the Example

```bash
uv run elspeth run -s examples/audit_export/settings.yaml --execute
```

### With Signed Exports (Legal/Compliance Use)

```bash
export ELSPETH_SIGNING_KEY="your-secret-key"
# Update landscape.export with:
#   authentication_policy: required
#   signing_mode: hmac_sha256
#   signer_key_id: example-key-v1
#   signing_secret_ref: ELSPETH_SIGNING_KEY
uv run elspeth run -s examples/audit_export/settings.yaml --execute
```

Verify the signed JSON delivery without access to the Landscape database:

```bash
elspeth audit-export verify examples/audit_export/output/audit_trail.json \
  --key-ref example-key-v1=ELSPETH_SIGNING_KEY
```

The checked-in example is explicitly unsigned. Its integrity-only verification
therefore requires `--allow-unsigned`.

Successful verification prints an `artifact_digest` for the verifier-owned
snapshot it checked. The command does not lock the source path after capture;
keep the export under stable custody, or verify it again and match that digest
immediately before another process consumes the source.

## Output Format

Uses JSON format because audit records are heterogeneous (run, node, row, token
records have different fields). CSV delivery is also supported as a portable
bundle containing its authenticated record stream, manifest, and one
deterministic projection per record type.

## Troubleshooting

### Schema Compatibility Error

If you see an error like:

> SchemaCompatibilityError: Landscape database schema is outdated

This means you have an old `audit.db` from a previous version. Fix by deleting it:

```bash
rm examples/audit_export/runs/audit.db
```

Then re-run the example.
