# Azure Document Intelligence expansion — second review

**Verdict: CHANGES_REQUESTED.** The revision resolves several first-review findings, but the replacement publication argument and new RAG migration have concrete gaps. Task 3 also remains explicitly blocked on D6, as the plan correctly states.

Reviewed on 2026-09-20 against HEAD `f6df9d0d5db01cda6b87dcddbe929b92b0d210c2`. Four review lanes inspected current source and the revised plan. No implementation or live Azure acceptance was performed. Local controlled experiments checked HTTP chunk buffering and retention selection; no full suite was run. The implementation plan was not edited.

## What improved

- The new `retrieve_bounded` contract and measured-read tests address the input-allocation finding.
- D6 now treats missing-figures behavior as an evidence-dependent implementation gate. This resolves the planning ambiguity without claiming the missing live evidence exists.
- D4 selects deployment-declared auth modes and includes catalog, availability and serializer parity work. The original missing design decision is resolved; admission details remain below.
- Non-2xx binary/missing-MIME recording now has explicit descriptor and telemetry tests.
- PDF validation scope is explicitly narrowed by the recorded operator decision; PNG validation is substantially stronger. Model-specific examples, both Entra acceptance modes, retry-cost wording and credential cleanup are addressed.
- The old table-specific export and retention-grade findings do not apply to the new design. This review does not ask to restore that table. Task 7 correctly identifies its separate schema break, PostgreSQL gate and shared-database reset custody.

## Required changes

### V2-1 — P1: Ordinary transform output is not the durable audit record D1 assumes

**Plan:** D1's spec-coverage table, lines 54–55; Task 3 audit-link test, line 410.

D1 says the output row is recorded through the engine's ordinary token payload and that `explain_token` exposes its artifact fields. For an ordinary one-to-one transform, `core/landscape/execution/node_states.py:530–562` stores `output_hash`, not the output document; `contracts/audit.py:261` identifies `token_data_ref` as an expand/coalesce payload. `core/landscape/lineage.py:340–350` returns the source row, states and calls, not every transformed row. Scheduler payloads temporarily retain execution data but are scrubbed on terminalization. A sink can retain the final row, but a downstream transform can remove the generated fields first.

The HTTP descriptor proves retrieval. It does not itself prove successful storage, identify the configured output-field mapping, or preserve a figure's publication order independently of temporary/final rows. The proposed test compares the call descriptor with an in-memory returned row, so it misses the durability assumption.

**Amendment:** Preserve publication evidence through an existing durable channel, for example a structured manifest in `success_reason["metadata"]` written only after successful stores. Include digest, kind, size, MIME, figure ID/order and output-field mapping as needed. Alternatively, explicitly design a generic platform facility. Add a completed-run regression where a downstream transform drops the generated fields; retrieve the publication evidence through the real audit reader and resolve its blobs after scheduler cleanup. This preserves row-in/row-out and requires no plugin-owned table.

### V2-2 — P1: Deadline checks remain above a buffering layer

**Plan:** Task 1 line 231 and Task 3 line 399.

The existing `_consume_capped_response` uses `response.iter_bytes(chunk_size=min(cap + 1, 65536))` (`plugins/infrastructure/clients/http.py:449–450`). httpx coalesces small raw reads before yielding that decoded chunk. Checking time only before/after yielded chunks still permits a trickling peer to occupy the worker far beyond the deadline while avoiding the inactivity timeout.

Controlled installed-httpx experiment, using 65,536 one-byte raw yields:

```text
{'raw_reads_before_first_deadline_check': 65536, 'bytes': 65536}
```

**Amendment:** Enforce cancellation/deadlines below coalescing and content-decoding, or use a transport-level total timeout. Bound blocking reads by remaining time. Test tiny raw chunks, including compressed input that produces no immediate decoded output, and assert reading stops before exhausting the generator. A fake that emits whole 64 KiB chunks will not expose this. Retain the new per-output checks; they address the original success-loop defect.

Also correct line 412: after two downloads consuming 40% each, 20% remains, so the third GET should start and then expire. To assert no third GET, exhaust the budget between requests explicitly.

### V2-3 — P1: Chroma still performs remote initialization before audited preflight

**Plan:** Task 7 lines 507–509.

Keeping provider construction in `on_start` keeps Chroma's `HttpClient` creation, `get_collection()` and remote metadata validation there (`plugins/infrastructure/clients/retrieval/chroma.py:188–214`). Missing/unreachable collections can therefore fail before the new preflight operation and retry manager exist. Moving only `count()` does not move the actual startup failure boundary.

**Amendment:** Move connection, collection lookup and metadata validation into audited initialization/preflight; define cleanup on partial initialization and how followers obtain their collection handles under the leader-only readiness design. Test constructor/lookup failures using the real provider with an injected SDK, not only a fake whose construction always succeeds. Assert the failure's operation/call evidence and retry behavior.

### V2-4 — P1: RAG readiness cannot currently carry the proposed retry classification

**Plan:** Task 7 lines 508–509.

`CollectionReadinessResult` has only `collection`, `reachable`, `count` and free-text `message` (`contracts/probes.py:15–31`). Chroma's probe collapses connection, SDK and malformed-count errors into the same unreachable result (`retrieval/chroma.py:537–546`); Azure Search similarly collapses network, authorization and capacity failures. Merely adding operation/fencing arguments does not let the transform distinguish transient from permanent failure as the plan requires.

**Amendment:** Define typed failure propagation at the provider boundary: classified exceptions after failed-call recording, or an owned failure result. Specify network/503 retry behavior versus authorization, missing/empty collection, invalid metadata and SSRF denial. Audit-write failures must propagate separately. Do not recover categories by parsing message text. Add tests for both providers through the orchestrator's actual retry classifier.

### V2-5 — P2: A mislabeled successful PDF still enters audit and telemetry

**Plan:** Task 1 lines 225–228.

The revised rule records `text/*` bodies verbatim up to 10,000 characters regardless of request intent. A PDF returned from `get_bytes` with an erroneous `Content-Type: text/plain` is therefore copied into audit and telemetry before Task 3 rejects its MIME. An ASCII PDF is sufficient; no binary decoding trick is needed.

**Amendment:** Record every successful `get_bytes` response as a descriptor, including malformed or incorrectly labeled bodies. Keep separately defined bounded error envelopes and submit-response handling. Add a text-labeled PDF containing a sentinel and verify it is absent from both audit and telemetry.

### V2-6 — P2: Specify deployment-mode admission and defaults

**Plan:** D4 and Task 4b lines 466–468.

The Textract precedent is an alias-based `OPERATOR_PROFILED` plugin. DI is `USER_CONFIGURABLE`; `web/plugin_policy/validation.py:739–741` skips profile validation when no profile option is supplied. Filtering advertised modes alone does not enforce the deployment's mode set for manually authored or restored nodes. Conversely, adopting the existing profile path directly imports its required alias (`validation.py:470–478`), contrary to the promised undeclared-profile default.

**Amendment:** Specify the common admission check and include `web/plugin_policy/validation.py`, or explicitly scope D4 to discovery guidance rather than policy admission. Test an undeclared mode submitted through the real validation path. For a managed-identity-only schema, require an explicit mode or define validated default lowering; narrowing the enum while leaving `auth_mode="api_key"` as its default is inconsistent.

## Additional corrections and remaining evidence

- **D7 overstates retention safety.** Not enumerating a nested reference does not guarantee its content-addressed bytes are never deleted: the same hash may be an expired direct audit reference. A controlled PurgeManager test found a nested-only active reference was invisible, an expired direct reference to the same bytes made it eligible, and adding an active direct reference protected it. This disproves the platform-wide claim for publishers such as `blob_fetch` fetching JSON. It does not establish that a validated PDF/PNG can collide with canonical JSON audit payloads. Narrow the claim and document both leak and dangling-reference risks in the platform issue; the no-table ruling can remain.
- **Startup exception/backoff semantics need pins.** New Azure preflight 429/503 handling delegates to a retry manager that does not consume `Retry-After`. Define the startup cooldown behavior. Token acquisition failures must raise preflight errors, not return Task 4a's row-level `TransformResult.error`: the preflight runner ignores return values. Include startup and resume tests.
- **Get Model itself is supported.** Microsoft's [2024-11-30 REST reference](https://learn.microsoft.com/en-us/rest/api/aiservices/document-models/get-model?view=rest-aiservices-v4.0+(2024-11-30)) documents the proposed route, both auth schemes, and a prebuilt-model example. This does not constitute live endpoint/RBAC acceptance.
- **D6 remains a legitimate hard gate.** No live no-figures probe was run in this review. The plan should continue to mark dependent Task 3 work blocked until the recorded operator evidence arrives.
- **Align the spec before execution.** The revised plan itself identifies changed artifact-effect and retention semantics. Update the spec to reflect the recorded rulings so executors are not instructed to satisfy conflicting requirements. This does not require reopening those decisions.

The next revision should retain the bounded-read, explicit-auth and model-specific improvements while repairing the evidence and initialization assumptions above.
