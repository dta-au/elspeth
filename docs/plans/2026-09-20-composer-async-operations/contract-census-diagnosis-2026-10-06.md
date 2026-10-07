# Contract census diagnosis — read-only

Canonical diagnostic process completed exit 0. `contract-census-diagnosis.log`, `.exit` and `.json` retain raw output, exact sites and controls. No production source, census pin or amendment code was changed.

The live `scripts/check_contracts.py` canonical AST/alias scanner was run against a private exact HEAD source export and the unchanged candidate. HEAD `23822a7477624d6264c60fade0905c19e8c552d7` matches its pin; the candidate does not:

```text
HEAD:      soft=2513 boundary=59
candidate: soft=2511 boundary=59
```

The scanner matched a known `dict[str, Any]` annotation, excluded `dict[str, int]`, and its actual pin gate rejected a same-total `Any`→`object` form mutation. An AST function-identity mutation was also rejected. Candidate Python-tree hashes were identical before/after diagnosis.

| File / form | Pinned → candidate | Exact diagnosis |
|---|---:|---|
| composer_turn / dict[str, Any] | 0 → 1 | `_post_compose_updates` survives at the common detached turn, line817. The retired messages/recompose routes previously each had this annotation. Common extraction retains the metadata obligation; it does not make the remaining dictionary an owned semantic type. |
| operation_receipts / dict[str, Any] | 2 → 1 | The removed site is `operation_receipt_request_hash.normalized`, not its live settlement `values` dictionary. The shared operation_codec calls `request.model_dump(...)` without an annotation. Installed BaseModel.model_dump's measured return annotation is still `dict[str, Any]`; annotation removal alone establishes no owned-type risk retirement. |
| proposal_authority / Mapping[str, Any] | 6 → 4 | `_pipeline_private_arguments_hash` and `_pipeline_audit_payload_hash` moved to proposal_decoder. |
| proposal_decoder / Mapping[str, Any] | 0 → 2 | Both moved helper functions are canonical AST-identical to HEAD, excluding line/source-location attributes. This is relocation, not two removed risks. |
| routes/composer/compose / dict[str, Any] | 1 → 0 | Recompose's `_post_compose_updates` moved into the common turn. |
| routes/messages / dict[str, Any] | 1 → 0 | Send's `_post_compose_updates` moved into the common turn. |

The resulting two-site total reduction combines one duplicate route consolidation and one explicit normalizer-annotation removal. It must not be reported as two owned-type conversions.

After held amendment approval and source work, the owner should use an honest closed metadata update type for the known repair/ingress fields, retain the normalized DTO's actual type explicitly or perform a real owned-domain conversion, and refresh the final census through the canonical tool in the same change. Removing annotations, changing Any to object, aliases or fabricated boundary metadata to hide sites is not a fix. Existing hash helper relocation needs an explicit per-file pin delta; it needs no behavior invention. Final shared source churn can change these exact sites, so diagnosis is readiness evidence rather than permission to pre-pin the unfinished tree.

The existing canonical contracts readiness process remains exit1. Its complete downstream/whole-tree clearance and final frozen gate are still owed.
