# Azure Document Intelligence expansion — plan review

This is the first review of the superseded table-based design. See the [second review](2026-09-20-azure-document-intelligence-capability-expansion-review-v2.md) for the updated plan and current findings.

Verdict: **CHANGES_REQUESTED**.

Reviewed 2026-09-20 against HEAD `1bcbdf441c40ec4b29c9a0fe96d7de7b52b91a34` and the live working tree. The checkout contains concurrent, unrelated modifications, including availability policy changes; this is source inspection, not a frozen release validation. Four review lanes covered reality, architecture, quality, and systems. No implementation, database resets, or test suites were performed. The plan was not edited.

The separate `transform_artifacts` table is a reasonable design. Store-before-record-before-row ordering is sound for publication, and the filesystem payload store already fsyncs its file and directory. The required changes concern integration and guarantees around that design.

## Required amendments

### R1 — P1: Enforce the retrieval deadline on successful calls

Plan: [Task 4](2026-09-20-azure-document-intelligence-capability-expansion.md#task-4--generated-pdf-and-figure-artifacts), lines 315–326.

The plan creates `retrieval_deadline` but uses the capacity-retry helper and passes the full request timeout. In `src/elspeth/plugins/transforms/azure/document_intelligence.py:566–597`, the helper returns immediately on success and checks its deadline only after 429/503. Successful figure downloads can therefore continue beyond the retrieval budget.

Add checks before each retrieval and after it completes, clamp request timeouts to the remaining budget, and define enforcement during streaming: an httpx inactivity timeout alone is not a total transfer deadline. Test successful slow transfers and expiration between figures, not only capacity retries. An expired attempt must not publish a successful row.

### R2 — P1: Keep binary error responses out of audit and telemetry

Plan: Task 2, line 222.

The binary-descriptor rule applies only to 2xx; non-2xx retains existing parsing on the assumption that Azure returns small JSON errors. `src/elspeth/plugins/infrastructure/clients/http.py:261–290` base64-encodes binary or missing-content-type bodies as `_binary`. A binary 502 response therefore still copies bytes into the call payload and telemetry, contradicting the plan's global binary-body prohibition.

Apply descriptor-only recording to binary responses regardless of status. Explicitly define bounded JSON/text error handling. Add 4xx/5xx binary and missing-content-type cases, checking both audit and emitted telemetry.

### R3 — P1: Do not silently turn missing requested figures into success

Plan: Task 4, lines 307 and 318.

`parse_figure_ids` deliberately maps both an absent key and `[]` to an empty tuple. When figure artifacts were requested, a response without `figures` consequently succeeds with an empty output list; the metadata flag only records that this happened. The spec requires missing requested outputs to fail and forbids synthesizing empty requested content from malformed success responses.

Distinguish requested-list absence from an explicit empty list and optional, unrequested facet absence. Test all three cases. If Azure legitimately omits the key for zero figures, establish that service contract and resolve the spec's required-output semantics explicitly before choosing success behavior.

### R4 — P1: Include the snapshot export path

Plan: Task 1, lines 119 and 185.

Repository wiring plus an exporter record loop is incomplete. `src/elspeth/core/landscape/exporter.py:405–415` replaces the ordinary repository adapter with the transaction-bound model for public export. That model directly queries existing artifacts in `src/elspeth/core/landscape/export_read_model.py:473–479`.

Explicitly extend the snapshot read model, exporter protocol and adapter, and owned export record DTO/union in `contracts/export_records.py`. Otherwise the proposed new read method is unavailable on the production export path; querying a separate repository to compensate would leave the immutable snapshot. Test the default public exporter on a real terminal run with transform artifacts, including snapshot consistency.

### R5 — P1: Resolve auth-mode availability before implementation

Plan: Task 5, line 372.

The plan's conditional instruction to stop if all declared secrets are required is already triggered by current code. `web/catalog/service.py:490–502` produces flat requirements; `web/catalog/schemas.py:38–47` carries only field/candidates; `web/plugin_policy/availability.py:44–53` requires every entry. Availability is computed before there is a selected pipeline configuration.

Keeping the API-key requirement hides managed-identity deployments. Removing it advertises readiness for an unconfigured API-key deployment. Adding an unconditional client-secret requirement breaks the other modes.

Settle how deployment-supported auth modes are declared and selected. Include catalog, availability snapshots, and Composer DTO/serializer changes in the task. The explicit serializers in `web/composer/inventory_response_contracts.py` and `web/composer/tools/_generation_schema_response.py` also need the chosen semantics. Test actual policy visibility for all modes, missing credentials, and planner-facing parity; catalog construction tests alone are insufficient.

### R6 — P2: Complete retention's reproducibility bookkeeping

Plan: Task 1, line 184.

Adding artifact hashes to the reference UNION makes them purgeable and protects hashes shared with active runs, but does not identify affected runs or update their replay grade. These are independent queries in `core/retention/purge.py:325–399` and `core/landscape/reproducibility.py:177–246`. The hash of the audited HTTP descriptor is not the artifact-body hash.

Include artifact ownership in affected-run discovery and artifact bytes from replay-dependent nodes in replay-critical deletion detection. Test purging only the generated artifact while retaining other payloads: the bytes disappear and the run becomes `ATTRIBUTABLE_ONLY`. Failed deletion must preserve its grade. Without these changes, deletion can leave a false `REPLAY_REPRODUCIBLE` claim.

### R7 — P2: Make the input memory bound real

Plan: Task 3, lines 256–262.

The plan calls `max_document_bytes` a memory bound, but proposes retrieving bytes and then checking their length. `core/payload_store.py:135` performs an unbounded `fd.read()` through `retrieve`. A much larger stored document is allocated before rejection, amplified by concurrent rows.

Add a bounded retrieval operation that preserves integrity verification for accepted payloads, or revise the advertised guarantee. Test that an oversized payload stops reading at the bound rather than merely testing the eventual error result.

## Additional corrections

- **Use model-specific examples and acceptance cases.** Task 6 line 403 asks one transform to request PDF and figures together; Task 7 line 413 similarly assumes a supporting model without identifying one. Microsoft's [add-on documentation](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/concept/add-on-capabilities?view=doc-intel-4.0.0) states searchable PDF is available only with Read. Its [layout documentation](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/prebuilt/layout?view=doc-intel-4.0.0#figures) describes generated figures for Layout. The combined-model premise is unverified. Provide separate known-supported Read/PDF and Layout/figure cases, or supply live evidence of a model supporting both. Config validation alone cannot prove the example runs successfully.
- **Resolve malformed-output validation strength.** The proposed prefix plus trailer checks accept `%PDF-%%EOF` and a PNG signature directly followed by an IEND tail. The plan honestly calls this a tripwire, but the spec promises rejection of malformed outputs. Define structural validation and corrupt-middle tests, or explicitly narrow the agreed guarantee.
- **Match live auth coverage to the spec.** Task 7 requests one Entra mode; spec implementation step 5 calls for both managed identity and service principal in the protected environment.
- **State reset custody.** Task 1 step 10 should distinguish new disposable worktree databases from existing shared local/deployed databases. The latter still require operator authorization under the repository covenant; an epoch bump is not permission to delete them.
- **Correct retry-cost wording and lifecycle details.** D2's “one path where a retry is billed” overlooks poll failures and ambiguous submission outcomes. Preserve the chosen new-analysis policy while describing those costs. Wire the new auth provider's `close()` after worker shutdown and adapt the existing string-only API-key validator for optional credentials.

These findings do not require abandoning the separate-table design. Amend the integration tasks and required-output/timeout contracts before execution, then review those changes against the current source tree.
