# Power Automate implementation specification review

**Final specification verdict: GO.** The final archive-integrity and external-token-boundary repairs have been inspected and independently tested. S1–S3 and the earlier late-integration repairs remain resolved. No remaining missing-functionality blocker was identified against the amended plan. This verdict covers specification review and completed focused checks, not frozen whole-tree gates or a merge result.

The review covers the full current Power Automate production scope against the implementation plan, including configuration/authentication, source and sink plugins, shared bounded/audited transport, deadline sockets, archived construction and CLI loading, source-call verification, sink binding propagation and authority, web destination policy, response DTOs, and the operating guide/examples. Changes to live tenant acceptance are accepted as instructed: **there is no tenant test, tenant acceptance gate or live-service prerequisite for completing this implementation review.** The feature's users still configure their own flows and targets as described in the guide.

Base at inspection: `487ac85a3` (`Merge branch 'plan/power-automate-implementation-20261001' into release/0.8.1`), with the implementation uncommitted. `git check-ignore -v docs/reviews/2026-10-01-power-automate-implementation-spec-review.md` returned no output before writing this report.

## Findings discovered and resolved

### S3 — High: case-variant JSON media type bypasses canonical-domain admission

**Resolved on late-integration recheck.** The defect was that `clients/http.py::_parse_response_body` recognized JSON using case-sensitive substring matching, while `PowerAutomateOperationClient.post_json` and the source correctly accepted normalized case-insensitive `application/json`. Consequently `APPLICATION/JSON` received a binary audit sidecar; the new canonical-domain check validated that base64 sidecar rather than the page's parsed values. Unsafe integers and lone surrogates could pass the HTTP whole-page boundary and fail later during row accounting/canonicalization.

Independent same-body positive/negative control, using a complete read envelope with rows `[9007199254740992, {"record_id": "A"}]`:

```text
application/json HTTPPolicyError invalid_json
APPLICATION/JSON ADMITTED (9007199254740992, mappingproxy({'record_id': 'A'}))
```

**Required fix:** select Power Automate JSON parsing using the same exact normalized media type as protocol admission, preserving generic-client behavior. Add uppercase/mixed-case unsafe-integer and lone-surrogate controls, canonical positive cases, and assert complete decoded evidence is retained with no candidate emission on refusal.

**Recheck:** Power Automate now normalizes the exact media type before selecting JSON parsing; the generic branch is unchanged. Independent lowercase, uppercase and mixed-case-with-charset controls each refused unsafe integers and lone surrogates with `invalid_json`, admitted canonical integer 42, and retained exact complete decoded bytes in every case. The real CLI pipeline's whole-page negative controls are part of the completed late selection below.

### S1 — High: snapshot-backed runs cannot be verified

**Resolved.** `source_load_input_data` now retains `snapshot_for_resume: True` from admitted source configuration. LIVE and VERIFY share that identity metadata. Real CLI snapshot and non-snapshot matching verification, plus changed-source refusal, pass in the final integration selection. The exact parent operation input-hash check remains intact.

The real CLI integration test `test_replay_unset_secrets_and_verify_volatile_headers_publish_no_sink` completes LIVE and offline REPLAY, then VERIFY aborts before source HTTP with:

```text
Canonical source read policy has no archived source reads
```

The changed-source negative control fails for the same setup reason, so it does not yet prove actual changed-page rejection.

The source of the mismatch is explicit in the production path. LIVE snapshot materialization records source-load operation input with `snapshot_for_resume: True` in `engine/orchestrator/source_iteration.py` (approximately line 607). `prepare_verified_sources` in `source_replay.py` records only `source_load_input_data(source)`, omitting that flag. `core/landscape/execution/calls.py:1235–1245` matches archived operation calls using both operation type and exact `input_data_hash` (line 1241). Therefore the archived calls exist but are not bound to the current verification operation. This is the shipped recovery configuration, not an unsupported mode.

**Required fix:** produce identical source operation identity metadata for LIVE and VERIFY from the admitted configuration. Keep exact hash-based parent admission; do not remove the input hash check or broadly fall back to another operation. Rerun the real CLI passing and changed-source controls with `snapshot_for_resume=True`, retaining no downstream-start/no sink-auth/no sink-call assertions. Also cover the ordinary non-snapshot variant so the repair does not hardcode the snapshot case.

### S2 — Medium: source verification crosses the engine/plugin dependency boundary

**Resolved.** Strict parsing and depth validation now live in `contracts/json_parser.py`; the depth constant and closed policy enum live in `contracts/source_read_verification.py`. The L3 runtime factory checks exact builtin source identity and issues its policy. Engine code consumes the nominal capability and requires the exact closed enum. All moved parser callers were inspected, including HTTP, Dataverse, Azure retrieval/transforms, pricing and Composer. No compatibility alias or lint suppression was introduced.

At inspection, `engine/orchestrator/source_read_verification.py` imports:

- line 13: `plugins.infrastructure.clients.json_utils.parse_json_strict`;
- line 14: `plugins.infrastructure.power_automate.MAX_JSON_DEPTH`;
- line 24: `plugins.sources.power_automate.PowerAutomateSource`.

These are runtime imports from L2 into L3. The plan specifically requires lower-layer owned policy contracts and dependency-clean engine integration.

The actual repository scanner, `scan_layer_imports_file`, was exercised directly against this file. Its raw result contained three `rule_id='L1'` findings, each with `message='Upward import: L2/engine imports from L3/application (...)'`. Before trusting that result, the same scanner was run on two temporary controls:

```text
positive: engine/probe.py importing plugins.sources.power_automate -> one L1 finding
negative: engine/probe.py importing contracts.enums -> no findings
actual: source_read_verification.py -> L1 at lines 13, 14, 24
```

**Required fix:** put genuinely shared strict JSON/depth policy in an appropriate lower layer and supply exact builtin admission through a composition-owned registration/nominal authority seam. Do not replace the exact-class admission with an untrusted class-name check or introduce a lint suppression. Update all HTTP/parser callers when moving shared helpers, and rerun the actual import scanner plus focused verification/HTTP tests.

## Verification evidence and limits

Two independent focused pytest commands were completed using the implementation worktree's interpreter and both local source roots. Their completed exit codes and terminal summaries were read directly, without a pipeline:

| Selection | Process exit | Terminal summary |
| --- | --- | --- |
| `tests/integration/plugins/test_power_automate_pipeline.py -n 0` | 1 | `3 failed, 1 passed, 2 warnings in 6.64s` |
| `tests/integration/pipeline/test_power_automate_effect_recovery.py -n 0` | 0 | `9 passed, 2 warnings in 18.70s` |

The first failure in the pipeline module is a separate accounting assertion: it expects `rows_processed == 5`, while the measured result is `2`. That fixture sets source validation failure to `discard`; three invalid candidates do not become processed pipeline rows. This is not by itself evidence of lost source accounting. The test must assert the run's discard accounting and inspect the three durable validation records, instead of treating discarded source candidates as ordinary failed terminal tokens. The remaining failures are S1.

The passing recovery module includes controlled response loss after applied and rejected outcomes, original-member resume identity, unknown pending/expired/conflicting evidence refusal, concurrent same-ID deduplication with an intentionally unsafe negative fixture, and actual process kill after durable target application. These are measured emulator/engine proofs, not claims about an external service.

An additional attempted real-CLI OAuth control was initially interrupted by the concurrent parser move. Its final independent rerun completed successfully with fake SDK credentials, pinned controlled DNS and an HTTP emulator. Both modes renewed bearer tokens and returned reformatted JSON with volatile headers during VERIFY:

```text
managed_identity live 0
managed_identity verify 0 read_requests 2 write_requests 1 actions 1
service_principal live 0
service_principal verify 0 read_requests 2 write_requests 1 actions 1
```

The read/write/action values are cumulative across LIVE and VERIFY: VERIFY performed one additional read and no additional publication. No tenant was contacted.

The final completed focused pytest command selected the following modules with both worktree source roots on `PYTHONPATH` and `-n 0`:

- `tests/integration/plugins/test_power_automate_pipeline.py`
- `tests/unit/cli/test_power_automate_nonlive.py`
- `tests/unit/core/test_power_automate_nonlive_loading.py`
- `tests/unit/web/execution/test_validation_runtime.py`
- `tests/unit/engine/orchestrator/test_power_automate_source_verify.py`
- `tests/unit/plugins/infrastructure/clients/test_json_utils.py`

Raw completion: `pytest_exit=0`; `167 passed, 2 warnings in 15.05s`. The integration module now passes the corrected accounting assertions, including exact persisted malformed candidates and source discard decisions. Warnings are Typer/Click deprecations.

The actual layer-import scanner was rerun after repair with freshly written controls: engine-to-plugin positive produced one L1; engine-to-contracts negative produced `([], [])`; current `source_read_verification.py` produced `([], [])`. This resolves S2 against the real producer, rather than a substitute regex.

No broad suite, PostgreSQL suite, hosted CI or signing clearance is claimed by this review.

## Functional completeness assessment

| Requirement | Current assessment |
| --- | --- |
| HTTP-triggered source and sink registration/configuration | Implemented with separate classes/configs and the shared registry name |
| SAS/service-principal/explicit user-assigned MI | Lazy nominal authentication variants, fixed scope, no ambient chain; unit transport controls exist |
| Credential-safe config and audit metadata | PA config forces fingerprints even under the development override; HTTP policy uses closed errors and independently retained decoded bytes |
| Bounded snapshot pagination | Whole-page admission, stable snapshot, cursor-cycle/page/row limits and candidate-based budgets are implemented |
| Source schema and malformed candidate accounting | Source normalizes fields and applies row policy to mapping/scalar/null/list candidates; corrected real pipeline accounting and raw-discard assertions pass |
| Source snapshot resume | Real recovery tests restore without source hooks/reads, including actual process death |
| Offline archived source/sink construction | Separate archived models and explicit construction DTOs; CLI projects before environment expansion and keeps sink authority absent |
| Public Web replay/verify | Existing unsupported boundary is preserved; no new public mode is required. Private worker rejects invocation fields before preflight/secrets |
| Source VERIFY | Scoped complete canonical evidence and required call consumption implemented; snapshot/non-snapshot, changed data and both OAuth CLI controls pass after S1 repair |
| Per-member publication and exact receipts | Member ID/hash/descriptor validation, status-before-write, sticky rejection diversion and unknown refusal implemented |
| Transport authority and fencing | Full runtime factory bindings, safe config identity, per-attempt revocation, pre-send/post-return checks and rate-limit guard paths implemented |
| Absolute bounded transport | Public httpcore backend, shrinking connect/read/TLS/send budgets, explicit short-write loop and bounded decompression/capture implemented |
| Web operator destinations | Exact origins are propagated through settings/runtime policy/hash/snapshot/evidence and checked independently of secret wiring |
| Usable reference flows | Guide describes read snapshot/cursor behavior and Dataverse create-only alternate-key insertion with original receipt read-back |
| Tenant acceptance removal | Plan and guide use deterministic acceptance; no live tenant test module or tenant action is required |
| Inventories, broad gates and integration merge | Registry/matrix/config changes inspected; whole-tree gates and merge remain the owner's separate integration work |

The new origin check deliberately follows the existing local trained-operator bypass. A direct control produced two origin denials for a restricted snapshot and none for trained-operator authority. The latter is a trusted local composition root, not evidence of an exposed public-web bypass; no security finding is asserted from it.

## Final scope and limits

### Late integration recheck

The late recheck covered the original full checklist again and inspected these additional production repairs:

- **Ambient proxies (Q1):** both Power Automate HTTP client construction paths use `trust_env=False`; the lazy PA client does not allocate the generic ambient-proxy client. The generic policy retains its existing behavior. Tests exercise an unsupported proxy scheme with PA success and generic failure controls.
- **Canonical-domain refusal (Q2/S3):** canonicalization runs before response DTO hashing or row emission. Unsafe integers and lone surrogates produce value-free `invalid_json`, a null parsed sidecar and complete decoded-byte evidence. Exact normalized media selection closes the independently reproduced case bypass.
- **Terminal archive admission:** only `COMPLETED`, `COMPLETED_WITH_FAILURES` and `EMPTY` are admitted. Config/version/node/source-lifecycle/hash checks remain. The actual CLI exercises empty LIVE→REPLAY→VERIFY and quarantined completed-with-failures archives.
- **Quarantine replay:** `source_replay.py` thaws frozen nested arrays, validates all archived validation records and their hash/run/source/row/decision binding, reconstructs original carrier shapes, records the entire validation ledger under new error IDs, and links replay rows to those new IDs. VERIFY compares all validation decisions rather than discard decisions alone. Replay-of-replay and a genuine `{"_raw": ["bad"]}` collision control check distinct row identity despite identical stored carrier bytes.
- **SDK diagnostics (Q4, discovered by the quality reviewer):** `clients/vendor_logging.py` uses a `ContextVar` scope and permanent chained public record factory. Vendor messages, positional arguments, exception/stack information and locations are replaced before the prior factory or handlers can observe them. Acquisition, constructors and close are scoped; no process-wide logger-level suppression is used. Real SP/MI decorators, MSAL error/stack logging, a prior record factory and concurrent unrelated authentication are exercised.

The final quarantine repair also reconstructs the supported owned missing-field, extra-field and type-mismatch violations using the closed contract type registry. This avoids refusing ordinary structured quarantine evidence from existing sources or dropping that evidence during replay. The final logging repair handles the unnamed placeholder used by `logging.makeLogRecord`/`ProcessorFormatter`; actual configured console and JSON logging, stderr and SDK close failures are covered.

Independent completed late evidence:

| Selection | Completed evidence |
| --- | --- |
| PA HTTP policy plus archived loaders | Exit 0; `64 passed in 1.67s` |
| PA HTTP policy, source replay, PA VERIFY, real pipeline, archived loaders | Exit 0; `155 passed, 2 warnings in 22.57s` |
| Complete PA client module, including real SDK diagnostic controls | Exit 0; `28 passed in 1.21s` |
| Independent media matrix | Six unsafe cases refused, three safe controls admitted; all nine retained exact complete decoded bytes |
| Final changed-scope rerun after structured-violation and logging-placeholder repairs: complete PA client, source replay, PA VERIFY and actual pipeline | Exit 0; `137 passed, 2 warnings in 21.62s` |

The proof owner additionally reported completed exit 0 for the final pipeline/recovery/process-death selection. This reviewer read its complete log: `40 passed, 12 deselected, 2 warnings in 59.66s`. It includes all three genuine process-kill seams and the strengthened same-carrier/different-original-shape replay-of-replay oracle. This is corroborating owner evidence, distinguished from the independently executed selections above.

These selections were completed during late integration rather than on a frozen release tree. No broad suite was launched by this reviewer.

### Final archive and external-token recheck

The final review held the prior GO while two additional owner-discovered boundary defects were repaired. The full current client, nonlive/archive module and configuration loader were reread.

Archived invocation fields are no longer removed using default-bearing `pop`. Before intentional comparison exclusions, the loader requires the complete persisted top-level and concurrency key sets and a positive exact-integer worker limit. `admit_archived_invocation_settings` requires an exact string containing a defined run mode, a string-or-null replay reference and a nonempty reference for nonlive modes. Database archive admission additionally matches the mode and applicable replay reference to the persisted run record. The existing producer's unused string reference under LIVE is preserved. Controls corrupt real Landscape records and recompute their hashes: missing fields and seven malformed/mismatched invocation values still refuse before secret access; a valid producer-generated LIVE reference remains accepted.

OAuth token admission now reads the external SDK object's token attribute once with a sentinel, validates an exact nonempty ASCII string without whitespace/control characters, and constructs a frozen owned token with no value-bearing representation. It does not nominally classify a third-party SDK class. Attribute access and parsing remain within the scoped SDK diagnostics and closed exception boundary. Controls include genuine SDK and dynamic external values, malformed/missing attributes, one-read behavior, header safety and raising properties.

Independent completed final evidence:

| Selection | Completed evidence |
| --- | --- |
| Complete PA client module after external token parsing repair | Exit 0; `44 passed in 1.17s` |
| Archived core loaders, CLI admission/corruption controls and actual PA pipeline | Exit 0; `93 passed, 2 warnings in 29.01s` |

These passes retain the earlier replay-of-replay, quarantine lineage, source VERIFY and no-publication behavior through the real pipeline selection. Subsequent integration with unrelated release commits and frozen whole-tree gates remain separate owner work.

Final inspection reconfirmed HEAD `487ac85a377f135e012bb206e3769cd65f6fadb8` with uncommitted implementation. Repair changes were inspected in full, including source-operation identity, exact factory policy issuance, shared parser consumers, early archived source config-hash binding, and Web execution/validation refusal before secrets. The initial accounting test issue and transient parser import mismatch are closed by the final completed selection.

The missing-functionality checklist above covers the requested source/sink, all authentication modes, archive construction, source verification, fencing/audit/transport, Web policy and usable remote reference behavior. The supported Web surface remains LIVE; preserving its existing refusal of invocation fields is deliberate and reflected in the amended plan, not a silently deferred new feature.

GO here does not certify every test or freeze the concurrently edited checkout. The owner must report required whole-tree/PostgreSQL gate results, local merge, push and hosted CI separately. There is **no live tenant acceptance condition**, opt-in tenant test requirement or deployment prerequisite attached to this verdict.
